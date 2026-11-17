"""Read-only recommendation knowledge, separate from scored catalogue formulae."""
from __future__ import annotations

import logging
import re

from services.guardian_point_knowledge import (
    _connect, database_name, parsed, scope_for, applicable, mentioned, names,
)

logger = logging.getLogger(__name__)
VERSION = 1
SOURCE = 'product_recommendation'
NOTE = '推荐来自模型与社区资料，尚未专业核验；功能概述是资料中的描述，不代表实际效果或已确认适合。'
GOAL_NEEDS = {
    'general': ['general_recommendation', 'food_selection', 'breed_food_selection', 'product_recommendation'],
    'coat_health': ['coat_health'], 'palatability': ['picky_eating', 'product_palability'],
    'weight_loss': ['weight_loss', 'weight_management'], 'weight_gain': ['weight_gain'],
    'soft_stool': ['soft_stool', 'digestive_sensitivity'],
    'black_chin': ['black_chin', 'chin_acne'], 'tear_stain': ['tear_stain'],
}
MEDICAL = re.compile(r'处方|兽医指导|医生指导|确诊|肾病|肾衰|心衰|尿结石|泌尿结石|水解蛋白|治疗|妊娠|哺乳|veterinary|prescription|renal|dietetic management', re.I)
STAGE_LABELS = {'adult': '成年', 'kitten': '幼猫', 'puppy': '幼犬', 'senior': '老年',
                'all_stages': '资料标注全阶段', 'unspecified': '资料未指明年龄'}


def compact(value):
    return re.sub(r'[^a-z0-9\u4e00-\u9fff]+', '', str(value or '').lower())


def load_material(pet, slots):
    """No schema bootstrap, imports, model calls, or writes in the serving path."""
    product_db = database_name('PRODUCT_KNOWLEDGE_DATABASE', 'chongxi_product')
    dictionary_db = database_name('GUARDIAN_DICTIONARY_DATABASE', 'chongxi_knowledge_dictionary')
    query = str(slots.get('target_requirement') or '')
    with _connect() as conn, conn.cursor() as cur:
        dictionaries = {}
        for table in ('dict_breed', 'dict_life_stage', 'dict_user_need'):
            cur.execute(f'SELECT * FROM {dictionary_db}.{table}')
            dictionaries[table] = cur.fetchall()
        # The profile owns the subject. Text may clarify age/breed but cannot
        # silently substitute a different species' records.
        scope = scope_for({'query': '', 'pet_scope': pet}, dictionaries)
        explicit_needs = {r['need_code'] for r in dictionaries['dict_user_need']
                          if any(mentioned(term, query) for term in names(r, 'need_name'))}
        generic = {'food_selection', 'food_transition', 'product_recommendation', 'product_analysis'}
        specific = explicit_needs - generic
        goal = slots.get('food_goal') or 'general'
        needs = set(GOAL_NEEDS.get(goal, []))
        if goal == 'general' and specific:
            needs = specific
        elif specific and specific <= {'digestive_sensitivity', 'soft_stool', 'diarrhea'}:
            needs = specific  # Sensitivity alone is not a soft-stool diagnosis.
        if not needs:
            return scope, dictionaries, []
        breeds = ['all_' + pet['animal_type'], 'unknown_' + pet['animal_type']]
        if scope.get('breed_code'):
            breeds.append(scope['breed_code'])
        cur.execute(f'''SELECT id,product_key,brand,product_name,entity_level,
            species_code,breed_code,life_stage_code,need_code,product_description,
            recommendation_reason,review_status,verification_status,sources
            FROM {product_db}.product_recommendation
            WHERE species_code=%s AND breed_code IN ({','.join(['%s']*len(breeds))})
            AND need_code IN ({','.join(['%s']*len(needs))})
            AND review_status <> 'rejected' AND verification_status <> 'rejected'
            ORDER BY id''', [pet['animal_type'], *breeds, *sorted(needs)])
        return scope, dictionaries, cur.fetchall()


def usable_sources(row, pet, stages):
    result = []
    for source in parsed(row.get('sources'), []):
        if not isinstance(source, dict):
            continue
        rec = source.get('recommendation') or {}
        original = source.get('original_source') or {}
        if not isinstance(rec, dict) or not isinstance(original, dict):
            continue
        if (rec.get('review_status') == 'rejected' or rec.get('evidence_status') == 'rejected'
                or source.get('source_evidence_status') == 'rejected'):
            continue
        condition = str(original.get('original_condition') or '')
        if not condition:
            conditions = (original.get('source_context') or {}).get('structured_item', {}).get('applicable_conditions', [])
            condition = '；'.join(str(c) for c in conditions) if isinstance(conditions, list) else str(conditions)
        # Unknown breed is unspecified context, not proof of breed suitability.
        check = dict(row, breed_code='all_' + pet['animal_type'] if row['breed_code'].startswith('unknown_') else row['breed_code'],
                     life_stage_codes=[row['life_stage_code']], original_condition=condition)
        reason = str(rec.get('recommendation_reason') or '').strip()
        if MEDICAL.search(' '.join([row['product_name'], condition, reason])):
            continue
        if row['life_stage_code'] in {'all_stages', 'unspecified'} and not re.search(r'全阶段|全年龄|all.life.stages', condition, re.I):
            restrictions = {code for code, pattern in [('adult', r'成猫|成年|成犬'),
                ('kitten' if pet['animal_type'] == 'cat' else 'puppy', r'幼猫|幼犬'), ('senior', r'老年')]
                if re.search(pattern, condition)}
            if restrictions:
                check['life_stage_codes'] = sorted(restrictions)
        if not applicable(check, pet, stages):
            continue
        if reason:
            result.append({'source_key': source.get('source_key'),
                           'provider': source.get('source_provider') or 'community',
                           'model': source.get('source_model'), 'question': source.get('source_question'),
                           'reason': reason, 'condition': condition})
    return result


def recommend(pet, slots, history=(), *, limit=3):
    base = {'version': VERSION, 'source_table': SOURCE, 'items': [], 'has_more': False, 'note': NOTE}
    def empty(status, note):
        return {**base, 'status': status, 'note': note}
    if pet.get('animal_type') not in ('cat', 'dog'):
        return empty('species_needed', '请先明确要为猫还是狗推荐产品。')
    if pet.get('diseases') or pet.get('allergies'):
        return empty('medical_diet_required', '档案有疾病或过敏记录，推荐资料不足以核验饮食限制，请先结合医生要求及完整配料表确认。')
    if slots.get('food_health_state') == 'vomiting' and slots.get('food_health_stage') != 'recovered':
        return empty('medical_diet_required', '目前有呕吐或恢复情况未明确，请先核对原因与饮食安排。')
    if slots.get('food_health_state') == 'gi_history' and slots.get('food_health_stage') != 'recovered':
        return empty('medical_diet_required', '肠胃炎尚未恢复稳定，请先结合诊疗安排确认饮食。')
    if slots.get('budget_per_jin') or slots.get('origin_type'):
        return empty('preference_unverified', '推荐库没有可核验的价格或产地信息，暂不能确认符合这次预算或产地要求。可以调整这些条件后继续推荐。')
    try:
        scope, dictionaries, rows = load_material(pet, slots)
        excluded = set(slots.get('_recommendation_exclude_ids') or [])
        eaten = {(compact(r.get('brand')), compact(r.get('product') or r.get('product_name')))
                 for r in [*history, {'brand': pet.get('food_brand'), 'product': pet.get('food_product')}]}
        current = compact(slots.get('current_food'))
        grouped = {}
        for row in rows:
            if row.get('review_status') == 'rejected' or row.get('verification_status') == 'rejected':
                continue
            if row.get('species_code') != pet['animal_type']:
                continue
            key = row.get('product_key')
            brand, product = str(row.get('brand') or '').strip(), str(row.get('product_name') or '').strip()
            if not key or key in excluded or not brand or not product:
                continue
            if slots.get('preferred_brand') and compact(slots['preferred_brand']) != compact(brand):
                continue
            if (compact(brand), compact(product)) in eaten or current and current in {compact(product), compact(brand + product)}:
                continue
            sources = usable_sources(row, scope, dictionaries['dict_life_stage'])
            if not sources:
                continue
            item = grouped.setdefault(key, {'product_key': key, 'brand': brand, 'product_name': product,
                'entity_level': row.get('entity_level') or 'product', 'source_ids': [], 'sources': [],
                'function_summary': '', 'life_stage_labels': [], 'cautions': [], 'breed_match': False})
            item['source_ids'].append(row['id'])
            item['breed_match'] |= row['breed_code'] == scope.get('breed_code')
            for source in sources:
                if source['source_key'] not in {s['source_key'] for s in item['sources']}:
                    item['sources'].append(source)
            label = STAGE_LABELS.get(row['life_stage_code'], row['life_stage_code'])
            if label not in item['life_stage_labels']:
                item['life_stage_labels'].append(label)
        items = list(grouped.values())
        # Deterministic relevance ordering; source counts are not clinical scores.
        items.sort(key=lambda i: (i['entity_level'] == 'brand', not i['breed_match'], -len(i['sources']), i['product_key']))
        for item in items:
            reasons = list(dict.fromkeys(s['reason'] for s in item['sources']))
            item['function_summary'] = '\n'.join(reasons[:2])
            providers = {s['provider'] for s in item['sources']}
            item['source_label'] = '模型与社区资料' if providers & {'community', 'file', 'screenshot', 'community_screenshot'} and providers - {'community', 'file', 'screenshot', 'community_screenshot'} else '社区资料' if providers <= {'community', 'file', 'screenshot', 'community_screenshot'} else '模型资料'
            item['stage_label'] = '、'.join(item.pop('life_stage_labels'))
            item['cautions'] = ['适用性及具体版本请核对包装与配料表']
            if item['entity_level'] == 'brand':
                item['cautions'].insert(0, '来源仅推荐品牌，未明确具体产品')
            if scope.get('age_months') is None:
                item['cautions'].append('未填写年龄，尚未确认年龄适配')
            if '资料未指明年龄' in item['stage_label']:
                item['cautions'].append('未指明年龄不代表全阶段适用')
            if slots.get('food_health_state') not in (None, 'none', 'unknown'):
                item['cautions'].append('当前有健康问题，资料推荐不代表已核验恢复期适配')
        count = max(1, min(int(limit), 3))
        return {**base, 'status': 'ready' if items else 'no_match', 'items': items[:count],
                'has_more': len(items) > count, 'matched_count': len(items),
                'note': NOTE if items else '当前条件下没有更多匹配的推荐资料，可以调整需求后再试；不会用其他物种或无关需求补足数量。'}
    except Exception as exc:
        logger.warning('guardian_product_recommendation_unavailable error=%s', type(exc).__name__)
        return empty('data_unavailable', '产品推荐资料暂时无法读取，请稍后重试。')


def reply_text(result):
    if not result['items']:
        return result['note']
    lines = ['按本次需求找到这些产品推荐资料：']
    for index, item in enumerate(result['items'], 1):
        name = '具体产品未明确' if item['entity_level'] == 'brand' else item['product_name']
        lines.extend([f'{index}. {item["brand"]} · {name}', item['function_summary'],
                      '；'.join(item['cautions'])])
    lines.append(result['note'])
    if result.get('has_more'):
        lines.append('可以说“换一组”查看其他推荐。')
    return '\n\n'.join(lines)
