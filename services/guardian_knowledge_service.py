"""Read-only, pet-scoped local knowledge QA for intents without an existing workflow."""
from __future__ import annotations

import json
import logging
import math
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pymysql
from openai import OpenAI
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from app_config import get_chat_model_config, get_mysql_config
from services.miniprogram_cat_profile_service import get_cat_profile
from services.miniprogram_diet_history_service import list_diet_history, compact_diet_context
from services.pet_health_models import get_db_url
from services import guardian_knowledge_cards as cards

logger = logging.getLogger(__name__)

STATE_KEY = '_knowledge_qa'
GENERAL = {'', '未知', '其他', '通用', '不限', '未限定', '全阶段', '全生命阶段', '所有', 'all', 'any'}
TOPIC_TERMS = {
    '口腔与清洁护理': ('刷牙', '口腔', '梳毛', '洗澡', '剪指甲', '清洁', '牙齿'),
    '行为与训练': ('抓挠', '沙发', '磨爪', '训练', '乱咬', '如厕', '焦虑', '玩耍', '陪玩'),
    '居住环境与安全': ('封窗', '环境', '新家', '新环境', '多猫', '多宠', '阳台', '温度'),
    '生命阶段': ('接宠', '到家', '新家', '幼猫', '幼犬', '老年', '断奶'),
    '用品选择标准': ('玩具', '猫砂', '食盆', '水碗', '猫抓板', '窝', '笼子'),
    '日常喂养': ('饮水', '喝水', '零食', '挑食', '喂食'),
    '驱虫与疫苗': ('驱虫', '疫苗'),
    '领养与品种': ('领养', '品种', '新手'),
}
ALIASES = {'沙发': ('抓挠', '猫抓板'), '磨爪': ('抓挠', '猫抓板'),
           '新家': ('新环境', '到家适应'), '喝水': ('饮水',), '乱咬': ('啃咬',)}
UNSAFE_SOURCE = re.compile(r'忽略.{0,12}(?:指令|规则|提示)|system\s*prompt|泄露.{0,8}(?:密钥|密码)|<\|(?:system|assistant)', re.I)
UNVERIFIED_MEDICAL = re.compile(r'剂量|用药|处方|治疗|治愈|致癌|中毒|抗生素|毫克|\bmg\b|'
                                 r'气球|橡皮筋|皮筋|塑料袋|线团|针线|精油|薰衣草|'
                                 r'(?:\d+(?:\.\d+)?|[一二三四五六七八九十两]+)\s*(?:次|片|粒|毫升|天|周|个月)')

# Lawsuit-grade negative assertions about products/industry — must not be shown
# as platform statements unless professionally verified.
NEGATIVE_CLAIM = re.compile(
    r'缺乏自有工厂|责任追溯|售后体系|品控背书|停产|断粮|断货风险|谨慎购买|不建议购买|不建议选|'
    r'避雷|智商税|网红粮|IP粮|重营销轻研发|概念包装|渠道推广|造假|虚标|塌房|维权|投诉|致死|致命|群体性')
# Group-user framing that single community posts cannot support.
GROUP_CLAIM = re.compile(r'多位用户|许多用户|大量用户|很多用户|不少用户|众多用户|被用户们')
# Mild negative experiences: brand names inside these sentences are generalised on output.
MILD_NEGATIVE = re.compile(
    r'软便|拉稀|腹泻|呕吐|便血|黑下巴|泪痕|眼屎|挑食|拒食|不爱吃|不适应|不适|翻车|出油|发酸|肥胖|过敏')
BRAND_TERMS = (
    '阿飞和巴弟', '坦克小希', '诚实一口', '金装素力高', '金素力高', '素力高', '伯纳天纯', '麦富迪',
    '弗列加特', '网易严选', '纽翠斯', '爱肯拿', '荒野盛宴', '帕缇朵', '法米娜', '归真', '纯皓',
    '恩萃', '蓝氏', '皇家', '伟嘉', '渴望', '百利', '福摩', '绿福摩', '巅峰', '冠能', '顽皮',
    '卫仕', '鲜朗', '江小傲', '高爷家', '小毛豆', '纯福', '醇粹', '比瑞吉', '比乐', '好主人',
    '力狼', '领先', '不二怪兽', '猫大圣', '猫友爱', '宽福', '有鱼', '玫斯', '宠率', '久生',
    '奶思', '希喂', '天赐力', '珍鱼堡', '诺乐', '钻石', '帕特', '星速', '简创', '泡芙', '蓝挚',
    '喵梵思', '星宴', '小嗷', '乐顿', '奇境', '本源', '希尔斯', '卡比', '天衡宝', '欧帝亿',
    '纽顿', 'GO', 'now', 'wowo', 'WOWO', 'E76', 'e76', 'whycat')
BRAND_PATTERN = re.compile(
    '|'.join(re.escape(term) for term in sorted(BRAND_TERMS, key=len, reverse=True)))


def scrub_brand_negatives(text):
    """Generalise brand names to '某品牌' inside sentences carrying negative claims.

    Positive recommendations keep brand names; only sentences whose content
    matches MILD_NEGATIVE get scrubbed, so useful recommendations survive.
    """
    parts = re.split(r'([。；;！？!?\n])', text)
    out = []
    for part in parts:
        if part and MILD_NEGATIVE.search(part):
            part = BRAND_PATTERN.sub('某品牌', part)
        out.append(part)
    return ''.join(out)


def _connect():
    return pymysql.connect(**get_mysql_config(), cursorclass=pymysql.cursors.DictCursor, autocommit=True)


def _json(value, fallback):
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value) if value else fallback
    except (TypeError, ValueError):
        return fallback


def load_health(pet_id):
    """Read the configured health database; never create tables or run health analysis."""
    engine = None
    try:
        url = get_db_url()
        parsed = make_url(url)
        if parsed.get_backend_name() == 'sqlite' and not Path(parsed.database or '').is_file():
            return {'status': 'unavailable', 'items': []}
        engine = create_engine(url, pool_pre_ping=True)
        today = datetime.now(timezone.utc).date()
        with engine.connect() as conn:
            rows = conn.execute(text('''SELECT metric_code,current_value,baseline_value,change_percent,
                anomaly_type,duration_days,target_date,detected_at FROM pet_metric_anomaly
                WHERE pet_id=:pet_id AND
                ((target_date BETWEEN :since AND :today) OR
                 (target_date IS NULL AND detected_at >= :since AND detected_at < :tomorrow))
                ORDER BY target_date DESC,detected_at DESC,id DESC LIMIT 20'''),
                {'pet_id': pet_id, 'since': today - timedelta(days=13), 'today': today,
                 'tomorrow': today + timedelta(days=1)}).mappings().all()
        return {'status': 'available' if rows else 'no_records', 'items': [dict(row) for row in rows]}
    except (SQLAlchemyError, OSError, ValueError):
        return {'status': 'unavailable', 'items': []}
    finally:
        if engine is not None:
            engine.dispose()


def load_context(user_id, pet_id):
    pet = get_cat_profile(user_id, pet_id)  # Ownership is checked before any other pet query.
    context = {'pet_profile': {key: pet.get(key) for key in (
        'id', 'name', 'animal_type', 'breed', 'sex', 'neutered', 'age_months', 'weight_kg',
        'food_brand', 'food_product', 'notes')}, 'diet_history': [], 'diet_history_status': 'unavailable'}
    try:
        history = list_diet_history(user_id, pet_id, limit=10)
        context.update(diet_history=compact_diet_context(history['items']),
                       diet_history_status='available' if history['items'] else 'no_records',
                       diet_history_has_more=bool(history.get('has_more')))
    except pymysql.MySQLError:
        pass
    context['health_anomalies'] = load_health(pet_id)
    return context


def plan_query(message, pet, previous=None):
    query = message[:600]
    if previous and len(message) < 35 and re.match(r'那|它|这个|这种|还要|还有|需要多久|怎么做|为什么', message):
        new_topics = {topic for topic, words in TOPIC_TERMS.items() if any(word in message for word in words)}
        if not new_topics or new_topics.intersection(previous.get('topics') or []):
            query = (previous.get('query', '')[:300] + ' ' + message)[:600]
    topics = [topic for topic, words in TOPIC_TERMS.items() if any(word in query for word in words)]
    terms = [word for words in TOPIC_TERMS.values() for word in words if word in query]
    if '玩' in query:
        terms.extend(('玩具', '互动', '玩耍', '逗猫棒'))
        topics = list(dict.fromkeys(topics + ['行为与训练', '用品选择标准']))
    for word, aliases in ALIASES.items():
        if word in query:
            terms.extend(aliases)
    anchors = list(dict.fromkeys(terms))
    cleaned = re.sub(r'我家|猫咪|狗狗|宠物|应该|怎么|如何|什么|怎么办|可以|需要|一下|请问|请|帮我|有没有|是不是|不爱|不喜欢|喜欢', ' ', query)
    for chunk in re.findall(r'[\u4e00-\u9fff]{2,}|[a-zA-Z]{3,}', cleaned):
        terms.extend(chunk[i:i + 2] for i in range(len(chunk) - 1))
    terms = list(dict.fromkeys(term for term in terms if term not in {'大家', '你好', '谢谢', '知道'}))[:18]
    species = pet.get('animal_type')
    if re.search(r'狗|犬', message) and re.search(r'猫', message):
        species = 'both'
    elif re.search(r'狗|犬', message) and not re.search(r'猫', message):
        species = 'dog'
    elif re.search(r'猫', message) and not re.search(r'狗|犬', message):
        species = 'cat'
    # An explicit different animal is not the selected pet's age/breed/health.
    scope = pet if species == pet.get('animal_type') else {'animal_type': species}
    return {'query': query, 'terms': terms, 'anchors': anchors, 'topics': topics, 'species': species, 'pet_scope': scope,
            'selection': bool(re.search(r'挑选|选择|怎么选|如何选|选购|怎么买', message))}


def _values(value):
    if isinstance(value, list):
        return [str(item).strip() for item in value]
    return [part.strip() for part in re.split(r'[、,，/|和与]', str(value or ''))]


def conditions_match(raw, pet):
    conditions = _json(raw, None)
    if not isinstance(conditions, dict):
        return False
    for key, value in conditions.items():
        values = _values(value)
        if all(v.lower() in GENERAL for v in values):
            continue
        if key == 'species':
            allowed = set()
            for v in values:
                if v in GENERAL or v in {'猫狗', '犬猫'}:
                    allowed.update(('cat', 'dog'))
                if v in {'猫', '猫咪', 'cat'}:
                    allowed.add('cat')
                if v in {'狗', '犬', '狗狗', 'dog'}:
                    allowed.add('dog')
            if (pet.get('animal_type') == 'both' and not {'cat', 'dog'}.issubset(allowed)
                    or pet.get('animal_type') != 'both' and pet.get('animal_type') not in allowed):
                return False
        elif key == 'life_stage':
            age = pet.get('age_months')
            if age is None:
                return False
            age = float(age)
            senior = 84 if pet.get('animal_type') == 'cat' else 96
            stage = '幼年期' if age < 12 else '成年期' if age < senior else '老年期'
            aliases = {'幼年': '幼年期', '幼猫': '幼年期', '幼犬': '幼年期', 'kitten': '幼年期',
                       'puppy': '幼年期', '成猫': '成年期', '成犬': '成年期', 'adult': '成年期', '老年': '老年期', 'senior': '老年期'}
            if stage not in {aliases.get(v, v) for v in values}:
                return False
        elif key in {'breed', 'sex', 'neutered'}:
            actual = pet.get(key)
            if key == 'sex':
                values = [{'公': 'male', '母': 'female'}.get(v, v) for v in values]
            if key == 'neutered':
                if actual is None or str(bool(actual)).lower() not in [str(v).lower() for v in values]:
                    return False
            elif actual is None or str(actual) not in values:
                return False
        else:
            return False  # Unsupported restrictions are never silently ignored.
    return True


def eligible(row, pet):
    professional = row.get('evidence_status') == '专业已验证' and row.get('status') != '待审核'
    if row.get('status') not in {'待审核', '已审核', '已通过', '已发布', 'active', 'approved', 'published'}:
        return False
    if row.get('evidence_status') not in {'待验证', '社区经验', '专业已验证'}:
        return False
    content = row.get('content') or ''
    if not content or len(content) > 2000 or UNSAFE_SOURCE.search(content):
        return False
    if re.search(r'产品线|旗舰店|优惠券|购买链接|某品牌', content):
        return False
    if not professional and UNVERIFIED_MEDICAL.search(content):
        return False
    if not professional and (NEGATIVE_CLAIM.search(content) or GROUP_CLAIM.search(content)):
        return False
    return conditions_match(row.get('conditions_json'), pet)


def _guide_content(row):
    """Render one second-stage guide into stable, human-readable evidence text."""
    sections = [f"问题：{row.get('problem')}"]
    mappings = (
        ('表现', _json(row.get('symptoms_json'), []), 'text'),
        ('风险提醒', _json(row.get('risks_json'), []), 'text'),
        ('争议与经验', _json(row.get('disputes_json'), []), 'text'),
    )
    for label, items, key in mappings:
        values = [str(item.get(key)).strip() for item in items if isinstance(item, dict) and item.get(key)]
        if values:
            sections.append(f"{label}：" + '；'.join(dict.fromkeys(values)))
    pairs = _json(row.get('pairs_json'), [])
    values = []
    for item in pairs:
        if not isinstance(item, dict):
            continue
        cause, action = item.get('cause'), item.get('action')
        if cause and action:
            values.append(f"{cause}；对应办法：{action}")
        elif cause:
            values.append(f"可能原因：{cause}")
        elif action:
            values.append(f"可参考办法：{action}")
    if values:
        sections.insert(2 if len(sections) > 1 else 1, '原因与办法：' + '；'.join(dict.fromkeys(values)))
    return '\n'.join(str(value) for value in sections if value and value != '问题：None')


def retrieve(plan):
    if not plan['terms']:
        return {'status': 'no_match', 'items': []}
    # Read only the second-stage guide table; never fall back to atomic knowledge_base rows.
    terms = [term.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') for term in plan['terms']]
    fields = ('k.problem', 'k.subtopic', 'k.symptoms_json', 'k.pairs_json', 'k.risks_json', 'k.disputes_json')
    clauses = ['(' + ' OR '.join(f'{field} LIKE %s' for field in fields) + ')'] * len(terms)
    score = ' + '.join([
        '(CASE WHEN k.problem LIKE %s THEN 2 ELSE 0 END) + '
        '(CASE WHEN k.symptoms_json LIKE %s OR k.pairs_json LIKE %s OR k.risks_json LIKE %s '
        'OR k.disputes_json LIKE %s THEN 1 ELSE 0 END)'
    ] * len(terms))
    params = [pattern for term in terms for pattern in ['%' + term + '%'] * len(fields)]
    params.extend(pattern for term in terms for pattern in ['%' + term + '%'] * 5)
    try:
        with _connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(f'''SELECT k.id,k.problem,k.topic,k.subtopic,k.symptoms_json,k.pairs_json,
                    k.risks_json,k.disputes_json,k.conditions_json,k.source_ids_json,
                    k.evidence_status,k.review_status AS status
                    FROM kd_problem_guides k
                    WHERE k.review_status IN ('待审核','已审核','已通过','已发布','active','approved','published')
                    AND k.evidence_status IN ('待验证','社区经验','专业已验证')
                    AND k.source_ids_json <> '[]'
                    AND ({' OR '.join(clauses)}) ORDER BY ({score}) DESC,
                    JSON_LENGTH(k.source_ids_json) DESC,k.id DESC LIMIT 300''', params)
                rows = cursor.fetchall()
        scored = []
        for row in rows:
            row['content'] = _guide_content(row)
            row['source_knowledge_ids'] = _json(row.get('source_ids_json'), [])
            row['source_count'] = len(row['source_knowledge_ids'])
            if not eligible(row, plan['pet_scope']):
                continue
            if plan.get('anchors') and not any(term in row['content'] for term in plan['anchors']):
                continue
            if plan.get('selection') and row.get('topic') != '用品选择标准':
                continue
            hits = [term for term in plan['terms'] if term in row['content']]
            if not hits:
                continue
            rank = sum(2 + min(len(term), 6) for term in hits) + (2 if row['topic'] in plan['topics'] else 0)
            rank += min(math.log1p(max(0, int(row.get('source_count') or 0))), 2)
            scored.append((rank, row))
        scored.sort(key=lambda item: (-item[0], -int(item[1]['id'])))
        unique, seen = [], set()
        for rank, row in scored:
            if row['content'] in seen:
                continue
            seen.add(row['content'])
            unique.append({**row, 'relevance': round(rank, 3)})
            if len(unique) == 5:
                break
        return {'status': 'matched' if unique else 'no_match', 'items': unique}
    except pymysql.MySQLError:
        return {'status': 'unavailable', 'items': []}


def structured_answer(message, context, plan, retrieval):
    rows = retrieval['items']
    if not rows:
        text_reply = ('本地养宠资料暂时无法读取，请稍后重试。' if retrieval['status'] == 'unavailable' else
                      '暂未找到适用于这个问题及宠物情况的可靠资料。请补充具体场景或换个问法，我再帮你查。')
        data = [cards.card('conclusion', text_reply)]
        return cards.plain_text(data), 'none', [], data, ''
    data, used_ids = cards.fallback(rows)
    mode = 'evidence_fallback'
    cfg = get_chat_model_config()
    if cfg.get('api_key'):
        try:
            payload = {'question': message, 'retrieval_query': plan['query'], 'question_species': plan['species'],
                       'context': context, 'knowledge': [
                           {key: row.get(key) for key in ('id', 'topic', 'subtopic', 'evidence_status', 'status', 'conditions_json')}
                           | {'evidence_lines': row['content'].splitlines()} for row in rows]}
            options = {'extra_body': {'thinking': {'type': 'disabled'}}} if cfg.get('provider') == 'deepseek' else {}
            client = OpenAI(api_key=cfg['api_key'], base_url=cfg['base_url'], timeout=14, max_retries=0)
            deadline = time.monotonic() + 22
            messages = [{'role': 'system', 'content': cards.SYSTEM},
                        {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False, default=str)}]
            for attempt in range(2):
                response = client.chat.completions.create(
                    model=cfg['model'], temperature=0.1, response_format={'type': 'json_object'},
                    max_tokens=2400, messages=messages,
                    timeout=max(0.1, min(14, deadline - time.monotonic())), **options)
                raw = response.choices[0].message.content
                try:
                    parsed = json.loads(raw)
                    data, used_ids = cards.validate(parsed, rows, UNSAFE_SOURCE, UNVERIFIED_MEDICAL)
                    break
                except (ValueError, TypeError, KeyError, AttributeError) as exc:
                    if attempt or deadline - time.monotonic() < 2:
                        raise
                    # One bounded format repair, using the same evidence and validation rules.
                    reason = str(exc) if type(exc) is ValueError else 'invalid JSON card schema'
                    messages.extend([{'role': 'assistant', 'content': raw}, {'role': 'user', 'content':
                        '格式校验未通过：' + reason + '。请修正并重新返回完整JSON；每个事实段须有真实行号引用，'
                        'content必须是字符串，结论不超过60字，不新增或补猜事实。'}])
            mode = 'model_structured_answer'
            if not data:
                data = [cards.card('conclusion', '现有相关资料还不足以回答这个问题。请补充具体场景，我再帮你查找适用资料。')]
                mode = 'insufficient_evidence'
        except Exception as exc:
            logger.warning('Knowledge answer fallback: %s', str(exc) if type(exc) is ValueError else type(exc).__name__)
            data, used_ids = cards.fallback(rows)
    # Retain the existing production wording guard in both card and legacy text output.
    for block in data:
        block['content'] = scrub_brand_negatives(block['content'])
        if block.get('questions'):
            block['questions'] = [scrub_brand_negatives(q) for q in block['questions']]
            block['content'] = '\n'.join('- ' + q for q in block['questions'])
    used = [row for row in rows if row['id'] in used_ids]
    note = cards.REVIEW_NOTE if any(row['evidence_status'] != '专业已验证' or row['status'] == '待审核' for row in used) else ''
    return cards.plain_text(data, note), mode, used_ids, data, note


def answer(message, context, plan, retrieval):
    # Keep the internal legacy helper contract for existing callers.
    reply, mode, ids, _, _ = structured_answer(message, context, plan, retrieval)
    return reply, mode, ids


def handle_question(user_id, conversation, message, state, *, report_scene=None):
    context = load_context(user_id, conversation['pet_id'])
    previous = state['slots'].get(STATE_KEY) or {}
    if previous.get('turn_no') != state['turn_count'] or previous.get('pet_id') != conversation['pet_id']:
        previous = None
    plan = plan_query(message, context['pet_profile'], previous)
    retrieval = retrieve(plan)
    reply, mode, used_ids, data, note = structured_answer(message, context, plan, retrieval)
    state.update(primary_intent='general_pet_qa', secondary_intent=None, current_step=None,
                 missing_slots=[], followup_count=0, turn_count=state['turn_count'] + 1)
    state['slots'][STATE_KEY] = {'query': plan['query'], 'topics': plan['topics'], 'pet_id': conversation['pet_id'],
                               'turn_no': state['turn_count'], 'knowledge_ids': used_ids, 'guide_ids': used_ids}
    response = {'response_type': 'text', 'reply': reply, 'result': {
        'intent': 'general_pet_qa', 'pipeline': 'knowledge_qa', 'turn_no': state['turn_count'], 'sequence_no': 1,
        'data': data,
        'knowledge_qa': {'version': 2, 'review_note': note, 'status': 'insufficient_evidence' if mode == 'insufficient_evidence' else retrieval['status'], 'generation_source': mode,
                         'query': plan['query'], 'topics': plan['topics'], 'knowledge_ids': used_ids,
                         'guide_ids': used_ids, 'source_table': 'kd_problem_guides',
                         'sources': [{key: row.get(key) for key in ('id', 'topic', 'subtopic', 'evidence_status', 'status', 'source_knowledge_ids')}
                                     for row in retrieval['items'] if row['id'] in used_ids],
                         'context_status': {'diet': context['diet_history_status'], 'health': context['health_anomalies']['status']}}},
        'disclaimer': 'AI内容仅供参考，不能替代兽医诊断'}
    if report_scene:
        from services.guardian_report_service import attach_report
        attach_report(response, context, scene=report_scene)
    return response
