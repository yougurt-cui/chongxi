"""Adapt the existing profile/score engine to real, current catalogue food candidates."""
from __future__ import annotations

import logging
import re
from math import isfinite

from app_config import get_feature_mysql_config
from services.taobao_sku_import_service import list_standardized_product_options

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
    text = ' '.join(str(slots.get(k) or '') for k in ('switch_reason', 'target_requirement', 'recent_symptom'))
    return next((name for name, pattern in REASONS if pattern.search(text)), None)


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
            adjusted = engine.build_adjusted_profiles_for_case(profiles, {}, goal, top_n=1)
            if not adjusted or not adjusted[0].get('weights'):
                return {'status': 'profile_missing', 'items': [], 'note': '这个目标尚无可用推荐画像，可先比较具体粮款。'}
            options = list_standardized_product_options(compare_available=True, limit=500).get('items', [])
            current_ids = {h.get('formula_id') for h in history if h.get('is_current') and h.get('formula_id')}
            current_name = (pet.get('food_brand'), pet.get('food_product'))
            catalog = {int(i['formula_id']): i for i in options if eligible_food(i, pet, slots)
                       and i['formula_id'] not in current_ids
                       and (i.get('brand'), i.get('product_name')) != current_name}
            if not catalog:
                return {'status': 'no_eligible_food', 'items': [], 'note': '暂未找到适用阶段、配料和偏好资料齐全的候选，可调整偏好或比较具体粮款。'}
            pool = engine.load_product_pool(db)
            if 'formula_id' not in pool.columns:
                return unavailable
            pool = pool[pool['formula_id'].isin(catalog)].drop_duplicates('formula_id')
            if pool.empty:
                return unavailable
            pool = engine.add_derived_features(pool)
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
            for rule in adjusted[0].get('avoid_rules') or []:
                if rule not in avoid_fields:
                    return unavailable
                fields += avoid_fields[rule]
            fields = list(dict.fromkeys(fields))
            if any(key not in pool.columns for key in fields):
                return unavailable
            # The UI engine fills missing metrics with neutral scores; the assistant must not.
            pool = pool.dropna(subset=fields)
            if pool.empty:
                return unavailable
            ranked = engine.recommend_products(pool, adjusted, top_n=limit)
            items = []
            for raw in engine.make_json_safe(ranked.to_dict('records')):
                if not raw.get('formula_id') or not raw.get('fit_score'):
                    continue
                item = catalog[int(raw['formula_id'])]
                items.append({k: item.get(k) for k in ('catalog_key', 'formula_id', 'product_id', 'brand', 'product_name', 'label', 'price', 'price_source', 'price_min_per_jin', 'price_max_per_jin', 'life_stage')})
                cautions = list(raw.get('cautions') or [])
                if _number(pet.get('age_months')) is None:
                    stage = str(item.get('life_stage') or '')
                    stage_label = {'kitten': '幼猫', 'adult': '成猫', 'senior': '老年猫',
                                   'all': '全阶段', 'all_stages': '全阶段', 'all life stages': '全阶段'}.get(stage.lower(), stage)
                    cautions.append('适用阶段：' + stage_label + '；年龄未填写，本次未按年龄筛选。')
                items[-1].update(fit_score=raw['fit_score'], strengths=raw.get('strengths') or [], cautions=cautions,
                                 profile_code=raw.get('matched_profile_code'), profile_name=raw.get('matched_profile_name'))
            return {'status': 'ready' if items else 'no_eligible_food', 'items': items,
                    'note': '候选来自当前配方与评分库，匹配分仅作参考；价格是品牌区间参考，不是实时售价。同期咨询不作为病因或负面吃粮反馈。',
                    'goal': goal, 'preferences': {k: slots[k] for k in ('budget_per_jin', 'preferred_brand', 'origin_type') if slots.get(k)}}
        finally:
            db.dispose()
    except Exception as exc:
        logger.warning('guardian_recommendation_unavailable error=%s', type(exc).__name__)
        return unavailable
