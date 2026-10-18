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

STATE_KEY = '_food_health_analysis'
RECOMMENDATION_KEY = '_health_recommendation_context'
CONFIRM_SLOT = 'food_health_recommendation'
CHOICE_SLOT = 'food_analysis_catalog'


def confirmation_interaction(message, state):
    snapshot = state.get('slots', {}).get(STATE_KEY) or {}
    if state.get('current_step') != CONFIRM_SLOT or snapshot.get('stage') != 'pending':
        return None
    text = str(message or '').strip().rstrip('。！!，,')
    if re.fullmatch(r'(?:暂时不需要|暂不需要|暂时不用|暂时不|暂不|先不用|先不推荐|不需要|不用|不要|不推荐|先不换粮|不了)(?:了)?', text):
        value = 'no'
    elif re.fullmatch(r'需要(?:推荐新粮)?|要|好的?|可以|推荐吧|推荐新粮|需要推荐|帮我推荐(?:一款)?(?:新粮|猫粮|主粮)', text):
        value = 'yes'
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
    if (analysis_id != snapshot.get('analysis_id') or not snapshot.get('analysis_id')
            or snapshot.get('stage') != 'pending' or state.get('current_step') != CONFIRM_SLOT
            or snapshot.get('user_id') != user_id or snapshot.get('pet_id') != conversation.get('pet_id')
            or snapshot.get('conversation_id') != state['conversation_id']):
        raise ValueError('这条分析的推荐选项已失效，请基于最新分析继续。')
    if not isinstance(value, str) or value not in {'yes', 'no'}:
        raise ValueError('请选择是否需要推荐新粮。')
    state['current_step'] = None
    if value == 'no':
        snapshot['stage'] = 'declined'
        state['turn_count'] += 1
        return {'response_type': 'text', 'reply': '好的，先保留这次分析。你可以继续问这款粮的特点，或补充实际吃粮表现。',
                'result': {'intent': 'food_analysis', 'analysis_id': snapshot['analysis_id'],
                           'analysis_stage': 'declined', 'food_candidates': [], 'turn_no': state['turn_count']}}
    snapshot['stage'] = 'recommended'
    state['slots'][RECOMMENDATION_KEY] = {k: deepcopy(snapshot[k]) for k in ('analysis_id', 'pet_id', 'subjects', 'scope')}
    # Analysis targets are references, never facts about what the pet is eating.
    state['slots'].pop('_food_flow', None)
    state['slots'].pop('target_food', None)
    state['slots']['_food_mode'] = 'selection'
    state.update(primary_intent='food_switch', secondary_intent=None, followup_count=0, missing_slots=[])
    return None


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
    parts = re.split(r'\s*(?:和|与| versus | vs\.? | VS |、)\s*', query, maxsplit=1)
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
            if len(parts) == 2:
                return {'response_type': 'text', 'reply': f'“{name}”还不能唯一定位到具体配方，请补充两款粮的完整名称或版本，再进行对比。',
                        'result': {'intent': 'food_analysis', 'food_candidates': []}}
            return {'response_type': 'food_select', 'reply': f'请确认要分析的具体粮款和版本：{name}。' if matches else f'暂未找到“{name}”的可核验配方，请补充完整名称或上传配料表。',
                    'interaction': {'slot': CHOICE_SLOT, 'options': [{'value': row['catalog_key'], 'label': row.get('label') or row['product_name']} for row in matches[:10]]} if matches else None,
                    'result': {'intent': 'food_analysis', 'food_candidates': []}}
        item = matches[0]
        subjects.append({'label': item.get('label') or item['product_name'],
                         'food_analysis': load_analysis(item['formula_id'])})
    context['named_food_analyses'] = subjects
    context['food_analysis_scope'] = 'named_comparison' if len(subjects) == 2 else 'named'
    if len(subjects) == 1:
        slots['catalog_key'] = f'formula:{matches[0]["formula_id"]}'
    return None


def build_analysis_reply(user_id, conversation, state, context):
    """All entry paths share the same report shape and confirmation protocol."""
    scope = context.get('food_analysis_scope') or 'catalog'
    subjects = []
    def append(analysis, label):
        product = (analysis or {}).get('product') or {}
        portrait = (analysis or {}).get('product_portrait')
        subjects.append({'label': label or ' '.join(filter(None, (product.get('brand'), product.get('product_name')))) or '所选粮款',
                         'formula_id': product.get('formula_id') or (portrait or {}).get('formula_id'), 'portrait': portrait})
    if scope in {'current', 'current_vs_history', 'catalog'}:
        current = context.get('current_food') or {}
        label = (' '.join(filter(None, (current.get('brand'), current.get('product_name')))) if scope.startswith('current') else None)
        if context.get('food_analysis') or scope.startswith('current'):
            append(context.get('food_analysis'), label)
    if scope in {'history', 'current_vs_history'}:
        for item in context.get('food_history_analyses') or []:
            record = item.get('diet_record') or {}
            append(item.get('food_analysis'), ' '.join(filter(None, (record.get('brand'), record.get('product') or record.get('product_name')))))
    for item in context.get('named_food_analyses') or []:
        append(item.get('food_analysis'), item['label'])
    if not subjects:
        state['current_step'] = None
        return {'response_type': 'food_select', 'reply': '还没有可以定位的分析对象，请提供具体粮款、选择历史口粮或上传配料表。',
                'result': {'intent': 'food_analysis', 'food_candidates': []}}
    knowledge = load_health_mappings()
    reports = []
    for item in subjects:
        reports.append({'label': item['label'], 'formula_id': item['formula_id'],
                        **analyse_portrait(item['portrait'], knowledge, health_state=state['slots'].get('food_health_state'))})
    comparison = []
    if scope in {'current_vs_history', 'named_comparison'} and len(subjects) > 1:
        for other in subjects[1:]:
            evidence = compare_portraits(subjects[0]['portrait'], other['portrait'])
            comparison.append({'left': subjects[0]['label'], 'right': other['label'], **evidence,
                               'text': portrait_comparison_text(evidence).replace('原粮', subjects[0]['label']).replace('目标粮', other['label'])})
    analysis_id = uuid.uuid4().hex
    snapshot = {'analysis_id': analysis_id, 'user_id': user_id, 'pet_id': conversation.get('pet_id'),
                'conversation_id': state['conversation_id'], 'scope': scope, 'stage': 'pending',
                'subjects': [{k: deepcopy(v) for k, v in r.items() if k != 'note'} for r in reports]}
    state['slots'][STATE_KEY] = snapshot
    state['slots'].pop(RECOMMENDATION_KEY, None)
    state.update(current_step=CONFIRM_SLOT, missing_slots=[], followup_count=0)
    lines = [report_text(item['label'], item) for item in reports]
    for item in comparison:
        lines.append(f'**{item["left"]} 与 {item["right"]} · 对比**\n' + item['text'])
    prompt = '是否需要根据上述内容推荐新粮？'
    lines.append(prompt)
    return {'response_type': 'result_card', 'reply': '\n\n'.join(lines),
            'interaction': {'slot': CONFIRM_SLOT, 'analysis_id': analysis_id, 'options': [
                {'label': '需要推荐新粮', 'value': f'yes:{analysis_id}'},
                {'label': '暂时不用', 'value': f'no:{analysis_id}'}]},
            'result': {'intent': 'food_analysis', 'analysis_id': analysis_id, 'analysis_stage': 'pending',
                       'health_analysis': {'version': 1, 'subjects': reports, 'comparisons': comparison, 'prompt': prompt},
                       'food_candidates': []}, 'disclaimer': 'AI内容仅供参考，不能替代兽医诊断'}
