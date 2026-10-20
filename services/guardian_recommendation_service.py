"""Adapt the existing profile/score engine to real, current catalogue food candidates."""
from __future__ import annotations

import logging
import re
from collections import Counter
import pymysql
from copy import deepcopy
from math import isfinite

from app_config import get_feature_mysql_config
from services.taobao_sku_import_service import list_standardized_product_options
from services.guardian_food_goals import GOALS, REFERENCE_PROFILES, goals_in_text
from services.guardian_food_analysis_service import (excluded_history_ids, reference_ids, recall_similar,
    load_nutrition, load_risks, analyse_food, comparison_reasons, FEATURE_LABELS, number, compact)
from services.guardian_food_scenario import build_scenario, assess_candidate, DIMENSIONS, score
from services.guardian_product_portrait_service import load_five_features, load_product_portraits, compare_portraits
from services.guardian_recommendation_presentation import select_candidates, compact_recommendation

logger = logging.getLogger(__name__)
REASONS = (
    ('soft_stool', re.compile(r'digestive|肠胃|软便|腹泻|拉稀')),
    ('black_chin', re.compile(r'黑下巴|下巴出油')),
    ('tear_stain', re.compile(r'泪痕')),
    ('weight_loss', re.compile(r'减重|减肥|控重|weight')),
    ('weight_gain', re.compile(r'增重|长肉')),
)


def _number(value):
    try:
        number = float(value)
        return number if isfinite(number) else None
    except (TypeError, ValueError):
        return None


def recommendation_goal(slots):
    if slots.get('food_goal') in GOALS:
        return slots['food_goal']
    text = ' '.join(str(slots.get(k) or '') for k in ('switch_reason', 'target_requirement', 'recent_symptom'))
    choices = goals_in_text(text)
    return choices[0] if choices else None


def reaction_from_note(note):
    text = str(note or '')
    if re.search(r'有没有|是否|不清楚|不知道|不确定|可能|好像|吗|[？?]', text):
        return None
    text = re.sub(r'(?:没有|无|未)(?:出现|发生|发现)?(?:不适|不耐受|过敏|软便|拉稀|呕吐)', '', text)
    if re.search(r'不适|不耐受|过敏|吃.{0,8}(?:软便|拉稀|呕吐)|便便不好', text):
        return 'problem'
    return 'good' if re.search(r'适应良好|便便正常|吃得适应|耐受良好|吃得稳定', text) else None


def explicit_preferences(message):
    text = str(message or "")
    result = {}
    match = re.search(r'(?:每斤|一斤)\s*(\d+(?:\.\d+)?)|(?:\b|预算)(\d+(?:\.\d+)?)\s*元\s*/\s*斤', text)
    if not match:
        match = re.search(r'(\d+(?:\.\d+)?)\s*元\s*(?:每斤|一斤|/斤)', text)
    if match:
        result['budget_per_jin'] = float(next(value for value in match.groups() if value is not None))
    if '国产' in text and '不限' not in text:
        result['origin_type'] = 'domestic'
    elif '进口' in text and '不限' not in text:
        result['origin_type'] = 'imported'
    return result


def eligible_food(item, pet, slots):
    """Age is optional; known ages still constrain catalogue life-stage matches."""
    kind = str(item.get('product_type') or '').strip().lower()
    if kind not in {'dry', 'dry_food', 'dry food', '主粮', '干粮', '膨化粮', '全价猫粮', '猫粮', '烘焙粮', '冻干主粮'}:
        return False
    stage = str(item.get('life_stage') or '').strip().lower()
    months = _number(pet.get('age_months'))
    all_stages = {'all', 'all_stages', 'all life stages', '全阶段', '全年龄', '全龄'}
    kitten = {'kitten', '幼猫', '幼年', '幼猫期'}
    adult = {'adult', '成猫', '成年', '成猫期'}
    senior = {'senior', '老年猫', '老年', '老年期'}
    if stage not in all_stages | kitten | adult | senior:
        return False
    if months is not None and not (stage in all_stages
            or months < 12 and stage in kitten
            or 12 <= months < 84 and stage in adult
            or months >= 84 and stage in senior):
        return False
    ingredients = str(item.get('ingredient_composition') or '')
    if not ingredients:
        return False
    aliases = {'鸡肉': ('鸡', 'chicken'), '鸡': ('鸡', 'chicken'), '鱼': ('鱼', 'fish', 'salmon', 'tuna'),
               '牛肉': ('牛', 'beef'), '牛': ('牛', 'beef'), '乳制品': ('奶', '乳', 'milk'),
               '谷物': ('米', '麦', '玉米', 'rice', 'wheat', 'corn')}
    for allergen in pet.get('allergies') or []:
        tokens = aliases.get(allergen)
        if not tokens:  # Unknown aliases cannot be certified absent in a formula.
            return False
        if any(token in ingredients.lower() for token in tokens):
            return False
    brand = str(slots.get('preferred_brand') or '').strip()
    if brand and brand != item.get('brand'):
        return False
    target = str(slots.get('target_food') or '').strip()
    if target:
        compact = lambda value: re.sub(r'[^a-z0-9\u4e00-\u9fff]+', '', str(value).lower())
        if compact(target) not in compact(str(item.get('brand') or '') + str(item.get('product_name') or '')):
            return False
    origin = slots.get('origin_type')
    origin = {'domestic': '国产品牌', 'imported': '进口/国际品牌'}.get(origin, origin)
    if origin and origin != item.get('origin_type'):
        return False
    budget = _number(slots.get('budget_per_jin'))
    if budget is not None:
        price = _number(item.get('price_max_per_jin'))
        if budget <= 0 or price is None or price > budget:
            return False
    return True


def recommend_guardian_foods(pet, slots, history=(), *, limit=3):
    result = _recommend_guardian_foods(pet, slots, history, limit=limit)
    logger.info('guardian_recommendation status=%s goal=%s audit=%s', result.get('status'), result.get('goal'), result.get('selection_audit'))
    return compact_recommendation(result)


def _recommend_guardian_foods(pet, slots, history=(), *, limit=3):
    if slots.get('food_health_state'):
        return recommend_scenario_foods(pet, slots, history, limit=limit)
    unavailable = {'status': 'data_unavailable', 'items': [], 'note': '推荐资料暂不可用，可以先对照配料表选粮。'}
    if pet.get('animal_type') != 'cat':
        return {'status': 'unsupported_species', 'items': [], 'note': '当前推荐库是猫粮库，这次先提供选粮方向。'}
    if pet.get('diseases'):
        return {'status': 'medical_diet_required', 'items': [], 'note': '档案有疾病背景，需要先结合医生的饮食要求筛选。'}
    goal = recommendation_goal(slots)
    if not goal:
        return {'status': 'goal_needed', 'items': [], 'note': '可以补充更希望改善的方向，再匹配现有推荐画像。'}
    try:
        from sqlalchemy import create_engine
        from sqlalchemy.engine import URL
        from consumer_apps import product_recommendation_engine as engine
        cfg = get_feature_mysql_config()
        db = create_engine(URL.create('mysql+pymysql', username=cfg['user'], password=cfg['password'],
                                     host=cfg['host'], port=cfg['port'], database=cfg['database'], query={'charset': 'utf8mb4'}),
                           connect_args={'connect_timeout': 8, 'read_timeout': 20})
        try:
            profiles = engine.load_recommendation_profiles(db)
            if goal in REFERENCE_PROFILES and not any(p.get('symptom_type') == goal for p in profiles.values()):
                profiles = {**profiles, goal: REFERENCE_PROFILES[goal]}
            adjusted = engine.build_adjusted_profiles_for_case(profiles, {}, goal, top_n=1)
            if not adjusted or not adjusted[0].get('weights'):
                return {'status': 'profile_missing', 'items': [], 'note': '这个目标尚无可用推荐画像，可先比较具体粮款。'}
            options = list_standardized_product_options(compare_available=True, limit=500).get('items', [])
            history = [dict(record) for record in history]
            feedbacks = slots.get('_food_history_feedbacks') or [slots.get('_food_history_feedback')]
            for feedback in feedbacks:
                if not feedback: continue
                feedback = str(feedback)
                text = compact(feedback)
                for record in history:
                    name = compact(record.get('product'))
                    if name and name in text:
                        record['note'] = feedback
                for option in options:
                    label = compact(str(option.get('brand') or '') + str(option.get('product_name') or ''))
                    if label and label in text and not any(record.get('formula_id') == option['formula_id'] for record in history):
                        history.append({'formula_id': option['formula_id'], 'brand': option.get('brand'), 'product': option.get('product_name'), 'note': feedback})
            excluded_ids = excluded_history_ids(pet, history, options, slots.get('_confirmed_history_text', ''))
            current_name = (pet.get('food_brand'), pet.get('food_product'))
            catalog = {int(i['formula_id']): i for i in options if eligible_food(i, pet, slots)
                       and i['formula_id'] not in excluded_ids
                       and (i.get('brand'), i.get('product_name')) != current_name}
            if not catalog:
                return {'status': 'no_eligible_food', 'items': [], 'note': '暂未找到适用阶段、配料和偏好资料齐全的候选，可调整偏好或比较具体粮款。'}
            audit = {'catalog_count': len(options), 'eligible_count': len(catalog), 'goal': goal}
            pool = engine.load_product_pool(db)
            if 'formula_id' not in pool.columns:
                return unavailable
            full_pool = engine.add_derived_features(pool.drop_duplicates('formula_id'))
            # The similarity table aliases fat regulation as 'antioxidant'; it
            # must never supply antioxidant evidence. Skin requires both inputs.
            if 'skin_protection' in full_pool and 'antioxidant_score' in full_pool:
                full_pool.loc[full_pool['antioxidant_score'].isna(), 'skin_protection'] = None
            current_ids = reference_ids({'formula_id': pet.get('food_formula_id'), 'brand': pet.get('food_brand'), 'product': pet.get('food_product')}, options)
            if not current_ids and slots.get('current_food'):
                current_ids = reference_ids({'product': slots['current_food']}, options)
            anchor_id = next(iter(current_ids)) if len(current_ids) == 1 else None
            current_rows = full_pool[full_pool['formula_id'] == anchor_id]
            current_row = current_rows.iloc[0].to_dict() if not current_rows.empty else {}
            history_notes = []
            combined = {'weight_adjust': {}, 'threshold_adjust': {}, 'notes': []}
            required_history = list(adjusted[0]['weights'])
            for record in history:
                ids = reference_ids(record, options)
                rows = full_pool[full_pool['formula_id'].isin(ids)]
                note = str(record.get('note') or '')
                reaction = reaction_from_note(note)
                if reaction and len(rows) == 1 and any(number(rows.iloc[0].get(key)) is not None for key in required_history):
                    adjustment = engine.derive_history_adjustment_from_features(rows.iloc[0], goal, reaction, record.get('product') or '')
                    for field, value in adjustment.get('weight_adjust', {}).items():
                        if number(rows.iloc[0].get(field)) is not None:
                            combined['weight_adjust'][field] = combined['weight_adjust'].get(field, 0) + value
                    for field, value in adjustment.get('threshold_adjust', {}).items():
                        if number(rows.iloc[0].get(re.sub(r'_(?:min|max)$', '', field))) is not None:
                            combined['threshold_adjust'][field] = combined['threshold_adjust'].get(field, 0) + value
                    history_notes.append('沿用已确认历史反馈：' + str(record.get('product') or '') + '，' + note[:100] + '；不推断症状原因。')
            if combined['weight_adjust']:
                adjusted = engine.apply_history_adjustments_to_profiles(adjusted, {'combined_adjustment': combined})
            pool = full_pool[full_pool['formula_id'].isin(catalog)]
            recall = {'status': 'not_required', 'items': []}
            if slots.get('_food_mode') == 'switch':
                recall = recall_similar(db, anchor_id, set(catalog))
                if recall['status'] == 'ready':
                    pool = pool[pool['formula_id'].isin([row['formula_id'] for row in recall['items']])]
            if pool.empty:
                return unavailable
            fields = [key for key, weight in adjusted[0]['weights'].items() if weight]
            fields += [re.sub(r'_(?:min|max)$', '', key) for key, value in (adjusted[0].get('thresholds') or {}).items() if value is not None]
            avoid_fields = {
                'protein_pressure_high': ['protein_pressure'], 'carb_pressure_high': ['carb_pressure'],
                'fat_pressure_high': ['fat_pressure'], 'fat_pressure_extreme_high': ['fat_pressure'],
                'p_buffer_low': ['p_buffer'], 'fiber_buffer_low': ['fiber_buffer'], 'q_feed_high': ['q_feed'],
                'q_feed_high_q_scfa_low': ['q_feed', 'q_scfa'], 'skin_protection_low': ['skin_protection'],
                'antioxidant_low': ['antioxidant_score'], 'fat_regulation_low': ['fat_regulation_score'],
                'protein_quality_low': ['protein_quality'],
            }
            missing = {key for key in fields if key not in pool or pool[key].isna().all()}
            for rule in adjusted[0].get('avoid_rules') or []:
                missing.update(key for key in avoid_fields.get(rule, []) if key not in pool or pool[key].isna().all())
            profile = deepcopy(adjusted[0])
            total_weight = sum(abs(value) for value in profile['weights'].values())
            covered_weight = sum(abs(value) for key, value in profile['weights'].items() if key not in missing)
            if not total_weight or covered_weight / total_weight < .5:
                return unavailable
            if goal == 'tear_stain' and 'antioxidant_score' in missing:
                return unavailable
            profile['weights'] = {key: value for key, value in profile['weights'].items() if key not in missing}
            profile['thresholds'] = {key: value for key, value in profile.get('thresholds', {}).items() if re.sub(r'_(?:min|max)$', '', key) not in missing}
            profile['avoid_rules'] = [rule for rule in profile.get('avoid_rules', []) if not set(avoid_fields.get(rule, [])) & missing]
            adjusted = [profile]
            fields = [key for key in fields if key not in missing]
            for rule in adjusted[0].get('avoid_rules') or []:
                if rule not in avoid_fields:
                    return unavailable
                fields += avoid_fields[rule]
            fields = list(dict.fromkeys(fields))
            if any(key not in pool.columns for key in fields):
                return unavailable
            # The UI engine fills missing metrics with neutral scores; the assistant must not.
            pool = pool.dropna(subset=fields)
            audit['complete_feature_count'] = len(pool)
            if pool.empty:
                return unavailable
            ranked = engine.recommend_products(pool, adjusted, top_n=len(pool))
            raw_rows = engine.make_json_safe(ranked.to_dict('records'))
            excluded_shown = set(slots.get('_recommendation_exclude_ids') or [])
            raw_rows = [row for row in raw_rows if row.get('formula_id') and (_number(row.get('fit_score')) or 0) > 0]
            available = [row for row in raw_rows if int(row['formula_id']) not in excluded_shown]
            audit['ranked_count'] = len(raw_rows)
            audit['unseen_count'] = len(available)
            if not available and raw_rows and excluded_shown:
                return {'status': 'alternatives_exhausted', 'items': [], 'goal': goal,
                        'note': '本轮符合条件的候选已展示完，没有降低筛选要求来更换产品。可以调整预算、品牌等偏好。'}
            selected = select_candidates([(catalog[int(row['formula_id'])], {'score': float(row['fit_score']), 'raw': row}, None)
                                          for row in available], limit)
            raw_rows = [row[1]['raw'] for row in selected]
            audit['returned_count'] = len(raw_rows)
            ranked = ranked[ranked['formula_id'].isin([row['formula_id'] for row in raw_rows])]
            nutrients = load_nutrition([anchor_id] + [int(value) for value in ranked.get('formula_id', [])])
            risks = load_risks([anchor_id] + [int(value) for value in ranked.get('formula_id', [])])
            similarities = {row['formula_id']: row['similarity'] for row in recall['items']}
            items = []
            for raw in raw_rows:
                if not raw.get('formula_id') or raw.get('fit_score') is None:
                    continue
                item = catalog[int(raw['formula_id'])]
                items.append({k: item.get(k) for k in ('catalog_key', 'formula_id', 'product_id', 'brand', 'product_name', 'label', 'price', 'price_source', 'price_min_per_jin', 'price_max_per_jin', 'life_stage')})
                cautions = list(raw.get('cautions') or [])
                if missing:
                    cautions.append('缺少' + '、'.join(FEATURE_LABELS.get(key, key) for key in sorted(missing)) + '资料；未计入本次评分，也未认定已通过相应风险筛选。')
                if _number(pet.get('age_months')) is None:
                    stage = str(item.get('life_stage') or '')
                    stage_label = {'kitten': '幼猫', 'adult': '成猫', 'senior': '老年猫',
                                   'all': '全阶段', 'all_stages': '全阶段', 'all life stages': '全阶段'}.get(stage.lower(), stage)
                    cautions.append('适用阶段：' + stage_label + '；年龄未填写，本次未按年龄筛选。')
                items[-1].update(fit_score=raw['fit_score'], strengths=raw.get('strengths') or [], cautions=cautions,
                                 profile_code=raw.get('matched_profile_code'), profile_name=raw.get('matched_profile_name'))
                candidate = full_pool[full_pool['formula_id'] == int(raw['formula_id'])].iloc[0].to_dict()
                items[-1]['analysis'] = analyse_food(item, nutrients, candidate, cautions, anchor_id, risks)
                items[-1]['current_food_reasons'] = comparison_reasons(current_row, candidate, goal)
                items[-1]['history_reasons'] = history_notes
                items[-1]['similarity'] = similarities.get(int(raw['formula_id']))
            return {'status': 'ready' if items else 'no_eligible_food', 'items': items,
                    'note': '候选来自当前配方与评分库，匹配分仅作参考；价格是品牌区间参考，不是实时售价。同期咨询不作为病因或负面吃粮反馈。',
                    'goal': goal, 'food_mode': slots.get('_food_mode', 'selection'), 'recall': recall,
                    'selection_audit': audit,
                    'history_note': '仅使用明确的历史好/坏反馈微调；缺少反馈或特征时只保留背景，不猜耐受表现。',
                    'missing_assessment_fields': sorted(missing), 'assessment_weight_coverage': round(covered_weight / total_weight, 4),
                    'current_analysis': analyse_food({'formula_id': anchor_id}, nutrients, current_row) if anchor_id else None,
                    'history_policy': 'exclude_all_confirmed_foods',
                    'preferences': {k: slots[k] for k in ('budget_per_jin', 'preferred_brand', 'origin_type') if slots.get(k)}}
        finally:
            db.dispose()
    except Exception as exc:
        logger.warning('guardian_recommendation_unavailable error=%s', type(exc).__name__)
        return unavailable


def recommend_scenario_foods(pet, slots, history=(), *, limit=3):
    """Five-dimensional preservation gates, then improvement ranking (requirement 16)."""
    scenario = build_scenario(slots)
    result = {'items': [], 'scenario': scenario, 'goal': slots.get('food_goal'),
              'food_mode': slots.get('_food_mode', 'selection'), 'history_policy': 'exclude_all_confirmed_foods'}
    def empty(status, note):
        return {**result, 'status': status, 'note': note}
    if pet.get('animal_type') != 'cat':
        return empty('unsupported_species', '当前推荐库是猫粮库，暂不能提供其他宠物的具体候选。')
    if pet.get('diseases'):
        return empty('medical_diet_required', '档案仍有疾病记录，需要先结合医生的饮食要求筛选；恢复情况不能自动清除诊断记录。')
    if scenario['status'] != 'ready':
        return empty(scenario['status'], scenario.get('note') or '还需要确认身体状态和当前阶段。')
    try:
        options = list_standardized_product_options(compare_available=True, limit=500).get('items', [])
        history = [dict(row) for row in history]
        for feedback in slots.get('_food_history_feedbacks') or []:
            text = compact(feedback)
            for option in options:
                label = compact(str(option.get('brand') or '') + str(option.get('product_name') or ''))
                if label and label in text and reaction_from_note(feedback):
                    matches = [r for r in history if reference_ids(r, options) == {option['formula_id']}]
                    if matches:
                        for record in matches:
                            record['note'] = feedback
                    else:
                        history.append({'formula_id': option['formula_id'], 'product': option.get('product_name'), 'note': feedback})
        excluded = excluded_history_ids(pet, history, options, slots.get('_confirmed_history_text', ''))
        catalog = {int(row['formula_id']): row for row in options
                   if row['formula_id'] not in excluded and eligible_food(row, pet, slots)}
        result['selection_audit'] = {'catalog_count': len(options), 'eligible_count': len(catalog),
                                     'goal': scenario['goal'], 'rejections': {}}
        if not catalog:
            return empty('no_eligible_food', '暂未找到满足适用阶段、配料和偏好且未吃过的粮款。')
        current_ids = reference_ids({'formula_id': pet.get('food_formula_id'), 'brand': pet.get('food_brand'),
                                     'product': pet.get('food_product')}, options)
        if not current_ids and slots.get('current_food'):
            current_ids = reference_ids({'product': slots['current_food']}, options)
        current_id = next(iter(current_ids)) if len(current_ids) == 1 else None
        reference_id, reference_source = None, None
        # User confirmation is tied to the exact food text shown when asked.
        if (slots.get('food_tolerance') == 'current_good' and current_id
                and slots.get('_tolerated_food') == slots.get('current_food')):
            reference_id, reference_source = current_id, 'user_confirmed_current'
        for record in history:
            if reference_id:
                break
            ids = reference_ids(record, options)
            if len(ids) == 1 and reaction_from_note(record.get('note')) == 'good':
                reference_id, reference_source = next(iter(ids)), 'confirmed_history_feedback'
        if scenario['keep'] and not reference_id:
            return empty('baseline_needed', '这个场景需要保留已耐受的配方特点，但还没有可确认的耐受参考粮。当前在吃不等于已经耐受；可补充一款吃得稳定的粮，或先比较具体产品。')
        # For healthy pets without preservation requirements, current food is only a comparison reference.
        reference_id = reference_id or current_id
        features = load_five_features([*catalog, current_id, reference_id])
        baseline = features.get(reference_id)
        if scenario['keep'] and (not baseline or any(baseline.get(k) is None for k in DIMENSIONS)):
            return empty('baseline_features_missing', '已确认耐受的参考粮缺少完整五维数据，暂不能核验哪些候选保留了这些特点。')
        ranked, rejected = [], Counter()
        for formula_id, item in catalog.items():
            assessment = assess_candidate(features.get(formula_id, {}), baseline, scenario)
            if not assessment['eligible']:
                rejected.update(assessment.get('failed_dimensions') or [assessment.get('reason', 'ineligible')])
                continue
            similarity = None
            if baseline and all(score(baseline.get(k), k) is not None for k in DIMENSIONS):
                similarity = round(1 - sum(abs(score(features[formula_id][k], k) - score(baseline[k], k))
                                           for k in DIMENSIONS) / len(DIMENSIONS), 4)
            ranked.append((item, assessment, similarity))
        ranked.sort(key=lambda row: (-row[1]['score'], -(row[2] or 0), row[0]['formula_id']))
        result['selection_audit'].update(scenario_match_count=len(ranked), rejections=dict(rejected),
                                         distinct_scores=len({row[1]['score'] for row in ranked}))
        shown = set(slots.get('_recommendation_exclude_ids') or [])
        unseen = [row for row in ranked if row[0]['formula_id'] not in shown]
        if ranked and not unseen:
            return empty('alternatives_exhausted', '本轮符合条件的候选已展示完，没有降低筛选要求来更换产品。可以调整预算、品牌等偏好。')
        ranked = select_candidates(unseen, limit)
        result['selection_audit']['returned_count'] = len(ranked)
        if not ranked:
            return empty('no_scenario_match', '暂未找到同时满足保持项和改善项、且五维数据齐全的候选；没有放宽稳定要求来凑推荐。')
        selected_ids = [row[0]['formula_id'] for row in ranked]
        nutrients = load_nutrition([current_id, *selected_ids])
        risks = load_risks([current_id, *selected_ids])
        portraits = load_product_portraits([current_id, *selected_ids])
        reference = next((row for row in options if row['formula_id'] == reference_id), {})
        result['reference'] = {'formula_id': reference_id, 'source': reference_source or 'current_comparison_only',
                               'label': reference.get('label') or reference.get('product_name'),
                               'tolerance_confirmed': bool(reference_source)}
        for item, assessment, similarity in ranked:
            cautions = list(scenario['notes'])
            if _number(pet.get('age_months')) is None:
                cautions.append('年龄未填写，本次未按年龄筛选；请核对包装适用阶段：' + str(item.get('life_stage') or '未知'))
            if scenario['health_state'] == 'black_chin':
                cautions.append('五维分只核验脂肪负担；脂肪来源复杂度和调节支持不计入本轮召回评分。')
            analysis = analyse_food(item, nutrients, cautions=cautions, current=current_id, risks=risks)
            analysis['five_dimensions'] = {k: features[item['formula_id']][k] for k in DIMENSIONS}
            analysis['product_portrait'] = portraits.get(item['formula_id'])
            analysis['portrait_comparison'] = compare_portraits(portraits.get(current_id), portraits.get(item['formula_id']))
            result['items'].append({**{k: item.get(k) for k in ('catalog_key', 'formula_id', 'product_id', 'brand',
                'product_name', 'label', 'price', 'price_source', 'price_min_per_jin', 'price_max_per_jin', 'life_stage')},
                'fit_score': assessment['score'], 'score_kind': 'scenario_improvement_reference',
                'strengths': assessment['improvement_reasons'] or ['满足本场景已核验的结构条件，专项效果未据此判断'],
                'keep_reasons': assessment['keep_reasons'], 'scenario_assessment': assessment,
                'cautions': cautions, 'analysis': analysis, 'similarity': similarity,
                'profile_code': scenario['version'], 'profile_name': scenario['label'],
                'current_food_reasons': [], 'history_reasons': []})
        return {**result, 'status': 'ready', 'note': '先验证保持项，再按改善项排序；相似度仅用于同分排序。五维结构分不是营养含量、适配概率或健康效果。',
                'recall': {'status': 'ready', 'feature_version': 'soft_v1', 'dimensions': list(DIMENSIONS)},
                'current_analysis': analyse_food({'formula_id': current_id}, nutrients, risks=risks) if current_id else None}
    except (pymysql.MySQLError, ValueError, TypeError, KeyError) as error:
        logger.warning('guardian_scenario_recommendation_unavailable error=%s', type(error).__name__)
        return empty('data_unavailable', '场景推荐资料暂时无法读取，请稍后重试。')
