"""Analysis snapshots and explicit, scoped handoff to the existing food flow."""
from __future__ import annotations

import re
import uuid
from copy import deepcopy

import pymysql
from services.guardian_health_analysis_service import load_health_mappings, analyse_portrait, report_text
from services.guardian_product_portrait_service import compare_portraits, portrait_comparison_text
from services.taobao_sku_import_service import list_standardized_product_options
from services.guardian_food_flow_service import resolve_target
from services.guardian_health_summary_service import build_overview

STATE_KEY = '_food_health_analysis'
RECOMMENDATION_KEY = '_health_recommendation_context'
CONFIRM_SLOT = 'food_analysis_usage'
RECOMMEND_SLOT = 'food_analysis_recommendation'
CHOICE_SLOT = 'food_analysis_catalog'


def confirmation_interaction(message, state):
    snapshot = state.get('slots', {}).get(STATE_KEY) or {}
    slot = state.get('current_step')
    if slot not in {CONFIRM_SLOT, RECOMMEND_SLOT} or snapshot.get('stage') != 'pending':
        return None
    text = str(message or '').strip().rstrip('。！!，,')
    if slot == RECOMMEND_SLOT:
        if re.fullmatch(r'需要(?:推荐(?:新粮)?)?|要|好的|好|可以|推荐吧', text):
            value = 'yes'
        elif re.fullmatch(r'暂时不用|不用(?:推荐)?|不需要(?:推荐)?|先不推荐', text):
            value = 'no'
        else:
            return None
        return {'slot': slot, 'value': value, 'analysis_id': snapshot['analysis_id']}
    if re.fullmatch(r'(?:已经|正在|目前|现在)?(?:在)?吃(?:这款粮)?|已经开始吃(?:了)?|正在吃(?:了)?', text):
        value = 'current'
    elif re.fullmatch(r'(?:还没|还没有|尚未)(?:开始)?吃|准备尝试|想试试|打算尝试', text):
        value = 'considering'
    else:
        return None
    return {'slot': CONFIRM_SLOT, 'value': value, 'analysis_id': snapshot['analysis_id']}


def accept_confirmation(interaction, user_id, conversation, state):
    """Validate before changing any state; stale/replayed buttons cannot recall."""
    snapshot = state.get('slots', {}).get(STATE_KEY) or {}
    value = interaction.get('value')
    analysis_id = interaction.get('analysis_id')
    if isinstance(value, str) and ':' in value:
        value, encoded_id = value.split(':', 1)
        if analysis_id is not None and analysis_id != encoded_id:
            raise ValueError('推荐选项与分析标识不一致，请基于最新分析继续。')
        analysis_id = encoded_id
    expected_slot = snapshot.get('confirm_slot') or (RECOMMEND_SLOT if snapshot.get('is_multiple') else CONFIRM_SLOT)
    if (analysis_id != snapshot.get('analysis_id') or not snapshot.get('analysis_id')
            or snapshot.get('stage') != 'pending' or state.get('current_step') != expected_slot
            or interaction.get('slot') != expected_slot
            or snapshot.get('user_id') != user_id or snapshot.get('pet_id') != conversation.get('pet_id')
            or snapshot.get('conversation_id') != state['conversation_id']):
        raise ValueError('这条分析的推荐选项已失效，请基于最新分析继续。')
    if expected_slot == RECOMMEND_SLOT:
        if not isinstance(value, str) or value not in {'yes', 'no'}:
            raise ValueError('请选择是否需要推荐新粮。')
        state['current_step'] = None
        snapshot['stage'] = 'confirmed' if value == 'yes' else 'declined'
        if value == 'yes':
            state['slots'][RECOMMENDATION_KEY] = deepcopy(snapshot)
            # A new recommendation request must not resume an old selected target.
            state['slots'].pop('_food_flow', None)
            state['slots'].pop('target_food', None)
            state['slots'].pop('_recommendation_scope', None)
            state['slots']['_food_mode'] = 'selection'
            state.update(primary_intent='food_switch', followup_count=0, missing_slots=[])
            return None
        state['turn_count'] += 1
        return {'response_type': 'text', 'reply': '好的，先保留这次分析。你可以继续问具体粮款或关注点。',
                'result': {'intent': 'food_analysis', 'analysis_id': analysis_id,
                           'analysis_stage': 'declined', 'food_candidates': [], 'turn_no': state['turn_count']}}
    if not isinstance(value, str) or value not in {'current', 'considering'}:
        raise ValueError('请选择猫咪是否已经在吃这款粮。')
    state['current_step'] = None
    snapshot['stage'] = value
    state['turn_count'] += 1
    reply = (
        '好的，已记录猫咪正在吃这款粮。你可以继续告诉我它最近的便便、食欲和消化表现。'
        if value == 'current' else
        '好的，已记录你准备尝试这款粮。开始时建议逐步换粮，并观察便便、食欲和消化表现。'
    )
    return {'response_type': 'text', 'reply': reply,
            'result': {'intent': 'food_analysis', 'analysis_id': snapshot['analysis_id'],
                       'analysis_stage': value, 'food_candidates': [], 'turn_no': state['turn_count']}}


def named_request(message):
    """Recognize explicit food analysis while leaving symptom triage to its router."""
    text = str(message or '').strip()
    if re.search(r'^(?:帮我|请|麻烦)?(?:分析|对比|比较)(?:一下)?', text):
        query = re.sub(r'^(?:帮我|请|麻烦)?(?:分析|对比|比较)(?:一下)?\s*', '', text)
    elif re.search(r'(?:这款|当前|现在|历史|以前|之前).{0,10}粮.*(?:怎么样|适合|特点|分析|对比)', text):
        query = text
    else:
        return None
    if not query or re.search(r'症状|病情|便血|精神|怎么治', query):
        return None
    if (re.search(r'软便|呕吐|腹泻|便秘|情况|状态|原因|化验|报告|行为|咳嗽|打喷嚏|掉毛|体重', query)
            and not re.search(r'粮|配方|食品|配料|原料|饲料', query)):
        return None
    query = re.sub(r'(?:有什么区别|有何区别|哪个更好|哪个好|哪款好|怎么样|适合它吗|适合吗)[？?。！!]*$', '', query)
    return query.strip(' “”。！？?!')


def prepare_named_analysis(message, state, context, load_analysis):
    """Resolve exact catalogue choices without silently choosing a formula version."""
    if context.get('food_analysis_scope') or context.get('food_submission'):
        return None
    slots = state['slots']
    if slots.get('catalog_key'):
        return None
    query = slots.get('food_name') or named_request(message)
    if not isinstance(query, str) or not query:
        return None
    parts = [part for part in re.split(r'\s*(?:和|与| versus | vs\.? | VS |、)\s*', query) if part]
    if len(parts) > 10:
        return {'response_type': 'text', 'reply': '一次最多分析10款粮，请缩小本次分析范围。',
                'result': {'intent': 'food_analysis', 'food_candidates': []}}
    subjects = []
    for name in parts:
        try:
            options = list_standardized_product_options(q=name, compare_available=True, limit=50).get('items') or []
            # The catalogue q searches brand/name separately; users commonly
            # send their concatenation (e.g. brand + product) in one phrase.
            if not options:
                options = list_standardized_product_options(compare_available=True, limit=500).get('items') or []
        except pymysql.MySQLError:
            return {'response_type': 'text', 'reply': '食品目录暂时无法读取，请稍后重试。',
                    'result': {'intent': 'food_analysis', 'food_candidates': []}}
        matches = resolve_target(name, options)
        if len(matches) != 1:
            state['slots']['_food_analysis_choices'] = [row['catalog_key'] for row in matches[:10]]
            state['current_step'] = CHOICE_SLOT
            if len(parts) > 1:
                state['current_step'] = None
                state['slots'].pop('_food_analysis_choices', None)
                return {'response_type': 'text', 'reply': f'“{name}”还不能唯一定位到具体配方，请补充各款粮的完整名称或版本，再进行对比。',
                        'result': {'intent': 'food_analysis', 'food_candidates': []}}
            return {'response_type': 'food_select', 'reply': f'请确认要分析的具体粮款和版本：{name}。' if matches else f'暂未找到“{name}”的可核验配方，请补充完整名称或上传配料表。',
                    'interaction': {'slot': CHOICE_SLOT, 'options': [{'value': row['catalog_key'], 'label': row.get('label') or row['product_name']} for row in matches[:10]]} if matches else None,
                    'result': {'intent': 'food_analysis', 'food_candidates': []}}
        item = matches[0]
        subjects.append({'label': item.get('label') or item['product_name'],
                         'food_analysis': load_analysis(item['formula_id'])})
    context['named_food_analyses'] = subjects
    context['food_analysis_scope'] = 'named_comparison' if len(subjects) > 1 else 'named'
    if len(subjects) == 1:
        slots['catalog_key'] = f'formula:{matches[0]["formula_id"]}'
    return None


def build_analysis_reply(user_id, conversation, state, context):
    """All entry paths share the same report shape and confirmation protocol."""
    scope = context.get('food_analysis_scope') or 'catalog'
    subjects = []
    def append(analysis, label, role='named', formula_id=None):
        product = (analysis or {}).get('product') or {}
        portrait = (analysis or {}).get('product_portrait')
        formula_id = product.get('formula_id') or (portrait or {}).get('formula_id') or formula_id
        label = label or ' '.join(filter(None, (product.get('brand'), product.get('product_name')))) or '所选粮款'
        key = ('formula', formula_id) if formula_id else ('unlinked', label)
        for subject in subjects:
            if subject['key'] == key:
                if role not in subject['roles']:
                    subject['roles'].append(role)
                return
        subjects.append({'label': label, 'formula_id': formula_id, 'portrait': portrait,
                         'key': key, 'roles': [role]})
    if scope in {'current', 'current_vs_history', 'catalog', 'saved_diet'}:
        current = context.get('current_food') or {}
        is_current = scope.startswith('current') or scope == 'saved_diet'
        label = ((' '.join(filter(None, (current.get('brand'), current.get('product_name')))) or '当前粮（未记录）') if is_current else None)
        if context.get('food_analysis') or scope.startswith('current') or (scope == 'saved_diet' and current):
            append(context.get('food_analysis'), label, 'current' if is_current else 'selected', current.get('formula_id'))
    if scope in {'history', 'current_vs_history', 'saved_diet'}:
        for item in context.get('food_history_analyses') or []:
            record = item.get('diet_record') or {}
            role = 'current' if scope == 'saved_diet' and record.get('is_current') else 'history'
            append(item.get('food_analysis'), ' '.join(filter(None, (record.get('brand'), record.get('product') or record.get('product_name')))), role, record.get('formula_id'))
    for item in context.get('named_food_analyses') or []:
        append(item.get('food_analysis'), item['label'])
    if not subjects:
        state['current_step'] = None
        return {'response_type': 'food_select', 'reply': '还没有可以定位的分析对象，请提供具体粮款、选择历史口粮或上传配料表。',
                'result': {'intent': 'food_analysis', 'food_candidates': []}}
    knowledge = load_health_mappings()
    reports = []
    for item in subjects:
        reports.append({'label': item['label'], 'formula_id': item['formula_id'], 'roles': item['roles'],
                        **analyse_portrait(item['portrait'], knowledge, health_state=state['slots'].get('food_health_state'))})
        reports[-1].setdefault('presentation', {'advantages': [], 'attentions': [], 'note': ''})
    comparison = []
    if scope in {'current_vs_history', 'named_comparison'} and len(subjects) > 1:
        for index, other in enumerate(subjects[1:], 1):
            evidence = compare_portraits(subjects[0]['portrait'], other['portrait'])
            comparison.append({'left': subjects[0]['label'], 'right': other['label'], 'subject_indices': [0, index], **evidence,
                               'text': portrait_comparison_text(evidence).replace('原粮', subjects[0]['label']).replace('目标粮', other['label'])})
    analysis_id = uuid.uuid4().hex
    multiple = len(subjects) > 1
    overview = build_overview(reports, comparison, use_model=False) if multiple else None
    coverage_note = ('本次使用最近10条口粮记录；还有更早记录未包含，请指定要继续分析的粮款。'
                     if scope in {'history', 'current_vs_history', 'saved_diet'} and context.get('diet_history_has_more') else '')
    if scope == 'saved_diet' and context.get('diet_history_status') == 'unavailable':
        coverage_note = '历史口粮暂时无法读取，本次仅解读已读取的当前粮，请稍后重试以包含历史记录。'
    confirm_slot = RECOMMEND_SLOT if multiple or scope == 'saved_diet' else CONFIRM_SLOT
    snapshot = {'analysis_id': analysis_id, 'user_id': user_id, 'pet_id': conversation.get('pet_id'),
                'conversation_id': state['conversation_id'], 'scope': scope, 'stage': 'pending', 'is_multiple': multiple, 'confirm_slot': confirm_slot,
                'subjects': [{k: deepcopy(v) for k, v in r.items() if k != 'note'} for r in reports]}
    state['slots'][STATE_KEY] = snapshot
    state['slots'].pop(RECOMMENDATION_KEY, None)
    state.update(current_step=confirm_slot, missing_slots=[], followup_count=0)
    lines = ([overview['text']] if overview else []) + [report_text(item['label'], item) for item in reports]
    if coverage_note:
        lines.append(coverage_note)
    for item in comparison:
        lines.append(f'**{item["left"]} 与 {item["right"]} · 对比**\n' + item['text'])
    ask_recommendation = confirm_slot == RECOMMEND_SLOT
    prompt = '是否需要根据上述内容推荐新粮？' if ask_recommendation else '你家猫已经在吃这款粮了吗？'
    lines.append(prompt)
    reply = {'response_type': 'result_card', 'reply': '\n\n'.join(lines),
            'interaction': {'slot': confirm_slot, 'analysis_id': analysis_id, 'options': ([
                {'label': '需要推荐新粮', 'value': f'yes:{analysis_id}'},
                {'label': '暂时不用', 'value': f'no:{analysis_id}'}] if ask_recommendation else [
                {'label': '已经在吃', 'value': f'current:{analysis_id}'},
                {'label': '还没有，准备尝试', 'value': f'considering:{analysis_id}'}])},
            'result': {'intent': 'food_analysis', 'analysis_id': analysis_id, 'analysis_stage': 'pending',
                       'health_analysis': {'version': 1, 'subjects': reports, 'comparisons': comparison, 'prompt': prompt,
                                           'overview': overview, 'is_multiple': multiple, 'coverage_note': coverage_note},
                       'food_candidates': []}, 'disclaimer': 'AI内容仅供参考，不能替代兽医诊断'}
    from services.guardian_report_service import attach_report
    return attach_report(reply, context, subjects)
