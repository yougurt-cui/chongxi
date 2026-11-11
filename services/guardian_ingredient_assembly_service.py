"""Read-only, version-scoped ingredient evidence for the guardian's current food.

No score, ingredient ordering or qualitative profile is treated as dosage or
clinical evidence. This service deliberately makes no model calls or DB writes.
"""
from __future__ import annotations

import json
import logging

import pymysql
import re
import unicodedata

from services.ingredient_science_profile_service import _connect

SLOT = '_current_food_ingredients'
CURRENT = re.compile(r'当前(?:吃的)?(?:口粮|猫粮|狗粮|粮|食物)|(?:现在|目前|正在|平时)(?:正|在)?吃的(?:口粮|猫粮|狗粮|粮)')
TOPIC = re.compile(r'原料|配料|成分|添加|含有|有没有|是否有|作用|区别|差别|贡献|含量|效果|为什么|干什么|干嘛|多少|占比|是什么|有.+吗')
FOLLOW = re.compile(r'它|这种|这个原料|该原料|那.*(?:区别|添加|作用)|营养贡献|实际效果')
CATEGORIES = {'protein': '蛋白质类', 'fat': '脂肪类', 'carbohydrate': '碳水化合物类', 'fiber': '纤维类', 'prebiotic': '益生元类', 'probiotic': '益生菌类', 'antioxidant': '抗氧化类', 'mineral': '矿物质类', 'vitamin': '维生素类', 'botanical': '植物来源类', 'other': '其他类'}
FUNCTIONS = {'protein_supply': '提供蛋白质', 'fat_supply': '提供脂肪', 'forming_support': '辅助粪便成形', 'bulk_support': '提供粪便容积支持', 'buffer_support': '提供纤维缓冲支持', 'microbiome_feed': '为肠道菌群提供可利用底物', 'scfa_support': '支持短链脂肪酸相关过程', 'omega3_support': '提供 Omega-3 相关营养支持', 'antioxidant_support': '提供抗氧化相关支持', 'micronutrient_support': '提供微量营养素'}
SUBTYPES = {'fresh': '鲜肉', 'frozen': '冻肉', 'meal': '肉粉', 'hydrolyzed': '水解形式', 'plant': '植物来源', 'animal': '动物来源', 'marine': '海洋来源', 'mixed': '混合形式', 'grain': '谷物', 'tuber': '薯类', 'legume': '豆类', 'flour': '粉类', 'refined_starch': '精制淀粉', 'soluble': '可溶性', 'insoluble': '不可溶性', 'inulin': '菊糖', 'oligosaccharide': '低聚糖', 'yeast_derived': '酵母来源', 'other': '其他', 'unknown_form': '形态待确认'}
FAMILIES = {'鸡肉': '鸡肉类', '鸭肉': '鸭肉类', '牛肉': '牛肉类', '鱼肉': '鱼肉类'}


def _json(value, default):
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def _norm(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(value or ''))).lower()


def is_request(message, state, pet_id):
    text = str(message or '')
    # Whole-food comparison and another animal/product belong to their own flows.
    if re.search(r'历史|以前吃|之前吃|换粮|推荐|另一款|上款粮|刚上传|刚投稿|刚买', text):
        return False
    previous = (state.get('slots') or {}).get(SLOT) or {}
    scoped = previous.get('pet_id') == pet_id and bool(previous.get('targets'))
    return bool((CURRENT.search(text) and TOPIC.search(text)) or
                (scoped and (FOLLOW.search(text) or (re.search(r'区别|差别|比较|呢[？?]?$', text) and not re.search(r'粮|产品|品牌|[A-Za-z0-9]', text)))))


def _catalog(cursor):
    cursor.execute('SELECT i.*,a.alias_names FROM csv_labeling.catfood_standard_ingredient i LEFT JOIN csv_labeling.catfood_standard_ingredient_alias a ON a.standard_ingredient_id=i.standard_ingredient_id WHERE i.active=1')
    return cursor.fetchall()


def find_targets(message, catalog):
    """Longest exact aliases win. Collisions are returned, never guessed."""
    text = _norm(message)
    spans = []
    for row in catalog:
        names = [row['standard_name']] + re.split(r'[、,，;；\n|]+', row.get('alias_names') or '')
        for name in names:
            token = _norm(name)
            if len(token) < 2:
                continue
            for match in re.finditer(re.escape(token), text):
                spans.append((match.start(), match.end(), token, row['standard_ingredient_id']))
    # Generic meat questions refer to the meat family, excluding isolated oils.
    for token in FAMILIES:
        for match in re.finditer(token, text):
            spans.append((match.start(), match.end(), token, 'family:' + FAMILIES[token]))
    selected = [s for s in spans if not any(a <= s[0] and b >= s[1] and b-a > s[1]-s[0] for a,b,_,_ in spans)]
    grouped = {}
    for start,end,token,identity in sorted(selected):
        ids = grouped.setdefault(token, set())
        ids.add(identity)
    results = []
    for token, ids in grouped.items():
        family = next((i for i in ids if i.startswith('family:')), None)
        results.append({'query': token, 'ids': [family] if family else sorted(ids)})
    return results[:3]


def label_amounts(raw, names):
    """Keep parent/subingredient percentages distinct; do not infer mass balance."""
    raw = unicodedata.normalize('NFKC', str(raw or ''))
    amounts = []
    for name in sorted(set(names), key=len, reverse=True):
        if not name:
            continue
        pattern = r'(?<![\w\u4e00-\u9fff])' + re.escape(name) + r'\s*(?:[:：]\s*)?(\d+(?:\.\d+)?)\s*%'
        for match in re.finditer(pattern, raw):
            prefix = raw[:match.start()]
            depth = sum(prefix.count(c) for c in '([【') - sum(prefix.count(c) for c in ')]】')
            item = {'label': match.group(0), 'percent': match.group(1), 'scope': 'compound_subingredient' if depth > 0 else 'whole_formula_label'}
            if item not in amounts:
                amounts.append(item)
    return amounts


def assemble(formula_id, targets, *, cursor=None, catalog=None):
    """Public reusable service. targets are resolved query/ids objects."""
    if cursor is None:
        with _connect() as conn, conn.cursor() as cur:
            return assemble(formula_id, targets, cursor=cur, catalog=catalog)
    catalog = catalog if catalog is not None else _catalog(cursor)
    if isinstance(targets, str):
        targets = find_targets(targets, catalog)
    by_id = {row['standard_ingredient_id']: row for row in catalog}
    cursor.execute('SELECT f.*,p.display_name,p.standard_product_name FROM csv_labeling.catfood_standard_formula f JOIN csv_labeling.catfood_standard_product p ON p.product_id=f.product_id WHERE f.formula_id=%s', (formula_id,))
    formula = cursor.fetchone()
    if not formula:
        return {'status': 'formula_missing', 'formula_id': formula_id, 'ingredients': []}
    cursor.execute('SELECT * FROM csv_labeling.catfood_formula_ingredient_item WHERE formula_id=%s ORDER BY position,item_id', (formula_id,))
    items = cursor.fetchall()
    fingerprint = formula.get('ingredient_fingerprint')
    fresh = bool(fingerprint and fingerprint == formula.get('active_ingredient_fingerprint') and formula.get('status') == 'active' and formula.get('confirmation_status') == 'confirmed')
    complete = bool(items) and fresh and all(not i.get('is_ignored') and i.get('standard_ingredient_id') and i.get('match_status') == 'matched' and i.get('review_status') in ('approved', 'auto_approved', 'confirmed') for i in items)
    packet = {'status': 'ok', 'formula_id': formula['formula_id'], 'formula_version': formula['formula_version'], 'ingredient_fingerprint': fingerprint, 'product_name': formula.get('display_name') or formula.get('standard_product_name'), 'is_current_version': bool(formula.get('is_current')), 'mapping_current': fresh, 'mapping_complete': complete, 'ingredients': []}
    for target in targets:
        identities = target['ids']
        family = identities[0][7:] if len(identities) == 1 and identities[0].startswith('family:') else None
        if len(identities) > 1:
            packet['ingredients'].append({'query': target['query'], 'presence': 'ambiguous', 'choices': [by_id[i]['standard_name'] for i in identities if i in by_id]})
            continue
        ids = [identity for identity, row in by_id.items() if row.get('ingredient_family') == family] if family else identities
        found = [i for i in items if i.get('standard_ingredient_id') in ids and not i.get('is_ignored')]
        # Raw-label mentions do not prove a standardized match or completeness.
        label_mention = target['query'] in _norm(formula.get('raw_ingredient_example'))
        verified = fresh and bool(found) and all(i.get('review_status') in ('approved', 'auto_approved', 'confirmed') and i.get('match_status') == 'matched' for i in found)
        entry = {'query': target['query'], 'presence': 'listed' if verified else ('unresolved' if found or label_mention or not complete else 'not_listed'), 'unresolved_reason': ('stale_mapping' if not fresh else 'label_only' if label_mention and not found else 'pending_review' if found else 'incomplete_mapping'), 'formula_items': [{'raw_name': i.get('raw_name'), 'standard_name': i.get('standard_name'), 'standard_ingredient_id': i.get('standard_ingredient_id'), 'position': i.get('position')} for i in found], 'profiles': [], 'label_amounts': []}
        for identity in dict.fromkeys([i['standard_ingredient_id'] for i in found] or ids):
            row = by_id.get(identity)
            if not row:
                continue
            cursor.execute("SELECT * FROM csv_labeling.catfood_ingredient_science_profile WHERE standard_ingredient_id=%s AND science_status='active' ORDER BY profile_version DESC,reviewed_at DESC,updated_at DESC,profile_id DESC LIMIT 1", (identity,))
            profile = cursor.fetchone()
            entry['profiles'].append({'standard_ingredient_id': identity, 'standard_name': row['standard_name'], 'ingredient_family': row.get('ingredient_family'), 'primary_nutrition_role': row.get('primary_nutrition_role'), 'profile_id': profile.get('profile_id') if profile else None, 'profile_version': profile.get('profile_version') if profile else None, 'nutrition_category': profile.get('nutrition_category') if profile else None, 'nutrition_subtype': profile.get('nutrition_subtype') if profile else None, 'domain_attributes': _json(profile.get('domain_attributes_json'), {}) if profile else {}, 'functions': _json(profile.get('function_attributes_json'), {}) if profile else {}, 'evidence_level': profile.get('evidence_level') if profile else 'unknown'})
        names = [i.get('raw_name') for i in found if i.get('raw_name')]
        entry['label_amounts'] = label_amounts(formula.get('raw_ingredient_example'), names)
        entry['quantification'] = {'inclusion': 'label_only' if entry['label_amounts'] else 'unknown', 'nutrient_contribution': 'insufficient_data', 'actual_effect': 'insufficient_data'}
        packet['ingredients'].append(entry)
    return packet


def render(packet):
    if packet['status'] != 'ok':
        return '当前口粮保存的配方记录未找到，请重新确认当前口粮；暂时无法核对原料。'
    lines = [f"按当前口粮「{packet.get('product_name') or '已保存产品'}」的配方 {packet['formula_id']}（版本 {packet['formula_version']}）核对："]
    if not packet['is_current_version']:
        lines.append('你保存的是这个产品的历史配方版本，本次按该版本回答。')
    for entry in packet['ingredients']:
        query = entry['query']
        presence = entry['presence']
        if presence == 'ambiguous':
            lines.append(f"“{query}”对应多个标准原料，请确认：{'、'.join(entry['choices'])}。")
            continue
        if presence == 'listed':
            lines.append(f"{query}：有标示，配料名称为「{'、'.join(dict.fromkeys(i['raw_name'] for i in entry['formula_items']))}」。")
        elif presence == 'not_listed':
            lines.append(f'{query}：当前配料明细未见标示；这不能作为无该原料或无过敏原的保证。')
        else:
            reason = {'stale_mapping': '当前配方与原料明细版本尚未核实一致', 'label_only': '标签有相关文字，但尚未核实对应的标准原料', 'pending_review': '有相关配料记录，但尚未完成审核', 'incomplete_mapping': '已核实配料未找到，但另有明细尚未完整映射'}
            lines.append(f"{query}：{reason.get(entry.get('unresolved_reason'), '配料映射尚不能完整核实')}，暂不能确认是否含有。")
        for profile in entry['profiles']:
            category = CATEGORIES.get(profile['nutrition_category'], '营养类别待补充')
            functions = [label for key,label in FUNCTIONS.items() if profile['functions'].get(key) in ('weak', 'medium', 'strong')]
            role = '、'.join(functions) or '现有已审核资料不足以说明具体作用'
            lines.append(f"{profile['standard_name']}属于{category}；现有原料画像支持的作用是：{role}。")
            if presence == 'listed' and functions:
                lines.append('这些作用可解释常见的添加目的；该产品的真实添加理由仍需厂家配方说明确认。')
        for amount in entry.get('label_amounts', []):
            scope = '复合配料内部比例，不能当作整款粮的添加量' if amount['scope'] == 'compound_subingredient' else '标签标示比例，不能直接等同于成品中的营养贡献'
            lines.append(f"标签注明「{amount['label']}」：{scope}。")
        if not entry.get('label_amounts'):
            lines.append('添加量：现有资料没有可核实的标示比例，不能按配料排序估算。')
    resolved = [e for e in packet['ingredients'] if e.get('profiles') and e['presence'] != 'ambiguous']
    if len(resolved) >= 2:
        a,b = resolved[:2]
        facts = []
        for e in (a,b):
            p=e['profiles'][0]
            facts.append(f"{p['standard_name']}：{CATEGORIES.get(p['nutrition_category'], '类别待补充')}，形态分类 {SUBTYPES.get(p['nutrition_subtype'], '未记录')}")
        lines.append('原料差异：' + '；'.join(facts) + '。仅凭原料名称和分类不能判断哪一种更好、吸收率或最终效果。')
    lines.append('营养贡献和实际效果：目前缺少原料实投量、批次营养检测及消化利用数据，不能算出贡献克数或贡献比例；实际效果还需要完整配方、喂食量和动物验证。画像里的功能等级和配方评分不能代替这些证据。')
    return '\n\n'.join(lines)


def handle_question(message, context, state, pet_id):
    try:
        return _handle_question(message, context, state, pet_id)
    except pymysql.MySQLError:
        logging.getLogger(__name__).exception('Current-food ingredient evidence unavailable')
        return _reply('暂时无法读取当前配方的原料资料，请稍后重试。此次无法核实原料，不能据此判断是否含有。', state, {'status': 'evidence_unavailable'})


def _handle_question(message, context, state, pet_id):
    food = context.get('current_food') or {}
    previous = (state.get('slots') or {}).get(SLOT) or {}
    formula_id = food.get('formula_id')
    if not formula_id:
        return _reply('当前口粮还没有关联可核对的配方 ID，请先确认并设置当前口粮。', state, {'status': 'current_formula_missing'})
    with _connect() as conn, conn.cursor() as cursor:
        catalog = _catalog(cursor)
        targets = find_targets(message, catalog)
        if (not targets and not CURRENT.search(message) and FOLLOW.search(message)
                and previous.get('pet_id') == pet_id and str(previous.get('formula_id')) == str(formula_id)):
            targets = previous.get('targets') or []
        if not targets:
            state.setdefault('slots', {}).pop(SLOT, None)
            return _reply('你想核对当前粮里的哪一种原料？请给我配料名称；比较时可以同时说出两种原料。', state, {'status': 'ingredient_needed'})
        if re.search(r'区别|差别|相比|比较', message) and len(targets) < 2:
            return _reply('请同时说出要比较的两种原料名称，我会分别核对是否在当前配方中，并比较已有原料资料。', state, {'status': 'comparison_target_needed'})
        if len(targets) > 2:
            return _reply('请一次指定一到两种原料，我会逐项核对当前配方。', state, {'status': 'too_many_ingredients'})
        packet = assemble(formula_id, targets, cursor=cursor, catalog=catalog)
    state.setdefault('slots', {})[SLOT] = {'pet_id': pet_id, 'formula_id': formula_id, 'targets': targets}
    return _reply(render(packet), state, packet)


def _reply(text, state, packet):
    state['turn_count'] = state.get('turn_count', 0) + 1
    # Keep other workflows resumable; only own this reply and its scoped reference.
    return {'reply': text, 'response_type': 'text', 'result': {'pipeline': 'current_food_ingredient_assembly', 'ingredient_evidence': packet}}
