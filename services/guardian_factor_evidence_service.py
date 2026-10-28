"""Factor-level evidence chain for guardian food comparison.

Why this module exists
----------------------
The comparison used to stop at the five structural scores. Those scores live
in a narrow band (protein 0.15-0.70, prebiotic 0.00-0.44 after the /5 scaling)
and were compared against fixed thresholds, so almost any pair of formulas
produced the same three-sentence verdict: "protein close + prebiotic low +
fat/carb lower". Nothing in the output explained *which* structural factor
maps to *which* symptom, and the soft-stool / black-chin models were never
consulted during a comparison.

This module keeps the scores but adds the two evidence layers that actually
differ between formulas:

1. ``position``   percentile of each structural score inside the whole pool,
                  so "close" is relative rather than an absolute band
2. ``risk_tags``  reason tags emitted by the soft-stool / black-chin models,
                  split into structural causes and gut-function mechanisms
3. ``community``  distilled comment evidence from ``kd_problem_guides``,
                  always labelled with its review status

Evidence boundaries
-------------------
* Risk tags are model output relative to a reference pool. They are not a
  probability, not a diagnosis and not a causal claim.
* Mechanism sentences are structural inference from the tag names, written so
  that a reader can see the chain factor -> mechanism -> symptom. They never
  claim an effect on a real cat.
* Community rows are unreviewed (``evidence_status=待验证``); they are shown
  only as "community also observed this", never as an instruction.
"""
import json
import logging
from bisect import bisect_right

import pymysql

from app_config import get_feature_mysql_config, get_mysql_config
from services.product_portrait_summary_service import DEFAULT_FEATURE_VERSION

logger = logging.getLogger(__name__)

MODULE_VERSION = 'factor-evidence-v1'

SYMPTOM_LABELS = {'soft_stool': '软便', 'black_chin': '黑下巴'}
MODEL_PREFIX = {'SOFT_STOOL': 'soft_stool', 'BLACK_CHIN': 'black_chin'}

# Structural scores are demand/pressure measures, not nutrition quality.
# Higher protein_score means more digestive demand, never "better protein".
FACTORS = {
    'carb': {'label': '碳水结构', 'dim': 'carb_score', 'higher': '压力更高'},
    'protein': {'label': '蛋白结构', 'dim': 'protein_score', 'higher': '压力更高'},
    'fat': {'label': '脂肪结构', 'dim': 'fat_score', 'higher': '负担更高'},
    'fiber': {'label': '纤维成形', 'dim': 'fiber_score', 'higher': '支持更足'},
    'prebiotic': {'label': '菌群与益生元', 'dim': 'prebiotic_score', 'higher': '支持更足'},
}
FACTOR_ORDER = ['carb', 'protein', 'fat', 'fiber', 'prebiotic']

# Tags emitted by the models, grouped by the structural factor they point at.
STRUCTURAL_TAGS = {
    '碳水结构压力偏高': 'carb',
    '碳水结构偏高': 'carb',
    '供菌底物相对过量': 'carb',
    '供菌底物偏多': 'carb',
    '蛋白结构偏复杂': 'protein',
    '蛋白消化压力偏高': 'protein',
    '脂肪负担偏高': 'fat',
    '脂肪消化负担偏高': 'fat',
    '脂肪来源较复杂': 'fat',
    '动物脂肪负担偏高': 'fat',
    '植物油脂干扰偏高': 'fat',
    '脂肪调节支持不足': 'fat',
    'Omega脂肪酸比例偏失衡': 'fat',
    'Omega-3支持不足': 'fat',
    'Omega-6压力偏高': 'fat',
    '纤维支持偏弱': 'fiber',
    '益生元支持偏弱': 'prebiotic',
}

# Tags that describe a gut-function mechanism rather than an ingredient pattern.
FUNCTION_TAGS = {
    '吸水成形能力偏弱': 'fiber',
    '粪便骨架支持偏弱': 'fiber',
    '便便成形支持不足': 'fiber',
    'SCFA支持偏弱': 'prebiotic',
    '菌群代谢支持不足': 'prebiotic',
    '肠道缓冲支持不足': 'prebiotic',
    '调节缓冲不足': 'prebiotic',
    '刺激缓冲能力偏弱': 'prebiotic',
    '抗氧化支持偏弱': 'prebiotic',
    '脂肪负担可能影响便便成形': 'fat',
}

# One plain sentence per tag so the reader can see where the factor leads.
MECHANISM = {
    '碳水结构压力偏高': '碳水来源结构对消化酶要求更高，未消化的部分会进入后段肠道',
    '碳水结构偏高': '碳水占比偏高，留给后段肠道的可发酵底物增多',
    '供菌底物相对过量': '可发酵碳水偏多，产气与渗透负荷上升',
    '供菌底物偏多': '可发酵底物偏多，结肠发酵强度上升',
    '蛋白结构偏复杂': '蛋白来源较复杂，对消化能力要求偏高',
    '蛋白消化压力偏高': '蛋白消化负荷偏高，未消化蛋白进入后段会改变菌群结构',
    '脂肪负担偏高': '脂肪总量偏高，对脂肪酶与胆汁的需求增加',
    '脂肪消化负担偏高': '脂肪类型与总量叠加，对消化系统压力偏高',
    '脂肪来源较复杂': '多种油脂并用，来源结构不单一',
    '动物脂肪负担偏高': '动物脂肪占比偏高',
    '植物油脂干扰偏高': '植物油脂比例偏高，脂肪酸构成偏离动物性来源',
    '脂肪调节支持不足': '皮脂调节相关的营养支持偏弱',
    'Omega脂肪酸比例偏失衡': 'Omega-6 与 Omega-3 比例偏离平衡',
    'Omega-3支持不足': '抗炎侧脂肪酸供给偏少',
    'Omega-6压力偏高': '促炎侧脂肪酸占比偏高',
    '纤维支持偏弱': '膳食纤维供给不足',
    '益生元支持偏弱': '可发酵纤维与低聚糖供给偏少',
    'SCFA支持偏弱': '短链脂肪酸产出偏少，结肠上皮供能偏弱',
    '菌群代谢支持不足': '菌群可用的发酵底物种类偏少，代谢产物结构单一',
    '肠道缓冲支持不足': '肠道对成分波动的缓冲余地偏小',
    '调节缓冲不足': '肠道自我调节的缓冲余地偏小',
    '刺激缓冲能力偏弱': '对刺激性成分的缓冲能力偏弱',
    '吸水成形能力偏弱': '粪便持水能力偏低，成型所需的结构支撑不足',
    '粪便骨架支持偏弱': '纤维骨架不足以维持粪便体积与形态',
    '便便成形支持不足': '成形相关的多个环节同时偏弱',
    '脂肪负担可能影响便便成形': '脂肪负担可能通过影响消化速度拖慢成形',
    '抗氧化支持偏弱': '抗氧化相关的营养支持偏弱',
}

# Community evidence lookup: factor (+ symptom) -> comment keywords.
COMMUNITY_KEYWORDS = {
    ('carb', 'soft_stool'): ['碳水', '淀粉', '谷物', '发酵', '胀气'],
    ('protein', 'soft_stool'): ['蛋白', '消化', '过敏'],
    ('fat', 'soft_stool'): ['脂肪', '油脂', '油腻', '油'],
    ('fat', 'black_chin'): ['油脂', '油', '黑下巴', '脂肪'],
    ('fiber', 'soft_stool'): ['纤维', '成形', '便便', '南瓜'],
    ('prebiotic', 'soft_stool'): ['益生元', '菌群', '益生菌', '肠道'],
}
MAX_COMMUNITY_PER_FACTOR = 2

# Community rows are only usable when the observation is about food itself.
# Without this gate a "菌群" keyword pulls in antibiotic dosing advice.
DIET_CONTEXT = ('粮', '食物', '配方', '喂', '吃', '饮食', '主食', '罐头', '干粮', '湿粮')
DRUG_CONTEXT = ('霉素', '注射', '给药', '药物', '抗生素', '针剂', '剂量', '处方药', '激素')


def _parse_tags(raw):
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(tag).strip() for tag in raw if str(tag).strip()]
    text = str(raw).strip()
    if not text:
        return []
    if text.startswith('['):
        try:
            return [str(tag).strip() for tag in json.loads(text) if str(tag).strip()]
        except (ValueError, TypeError):
            pass
    return [part.strip() for part in text.split(',') if part.strip()]


def _informative(tags):
    """Drop the "no obvious risk" placeholders; they carry no comparison value."""
    return [tag for tag in tags if tag and not tag.startswith('暂无明显')]


def _percentile(sorted_values, value):
    if not sorted_values or value is None:
        return None
    position = bisect_right(sorted_values, value)
    return round(position / len(sorted_values), 4)


def load_pool_positions(feature_version=DEFAULT_FEATURE_VERSION):
    """Percentile of every formula's structural score inside the whole pool."""
    dims = [meta['dim'] for meta in FACTORS.values()]
    columns = ','.join(['s.formula_id'] + ['s.%s' % dim for dim in dims])
    sql = f'''SELECT {columns}
        FROM sku_feature_input s
        WHERE s.feature_version=%s
          AND s.id=(SELECT MAX(v.id) FROM sku_feature_input v
                    WHERE v.formula_id=s.formula_id AND v.feature_version=s.feature_version)'''
    try:
        with pymysql.connect(**get_feature_mysql_config(), cursorclass=pymysql.cursors.DictCursor) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, [feature_version])
                rows = cur.fetchall()
    except pymysql.MySQLError as error:
        logger.warning('guardian_factor_pool_unavailable error=%s', type(error).__name__)
        return {}, {}
    values = {dim: [] for dim in dims}
    per_formula = {}
    for row in rows:
        try:
            formula_id = int(row['formula_id'])
        except (TypeError, ValueError):
            continue
        per_formula.setdefault(formula_id, {})
        for dim in dims:
            try:
                number = float(row[dim])
            except (TypeError, ValueError):
                continue
            values[dim].append(number)
            per_formula[formula_id][dim] = number
    for dim in values:
        values[dim].sort()
    positions = {}
    for formula_id, scores in per_formula.items():
        positions[formula_id] = {dim: _percentile(values[dim], score) for dim, score in scores.items()}
    return positions, {dim: len(values[dim]) for dim in dims}


def load_risk_tags(formula_ids):
    """Latest soft-stool / black-chin row per formula, tags split but unclassified."""
    ids = sorted({int(i) for i in formula_ids if i})
    if not ids:
        return {}
    marks = ','.join(['%s'] * len(ids))
    sql = f'''SELECT formula_id,score_model_version,current_pool_risk_level,
            main_reason_tags,support_reason_tags,fat_detail_tags,reference_pool_version,calculated_at
        FROM sku_risk_score_result
        WHERE formula_id IN ({marks})
          AND (score_model_version LIKE 'SOFT_STOOL%%' OR score_model_version LIKE 'BLACK_CHIN%%')
        ORDER BY calculated_at DESC'''
    try:
        with pymysql.connect(**get_feature_mysql_config(), cursorclass=pymysql.cursors.DictCursor) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, ids)
                rows = cur.fetchall()
    except pymysql.MySQLError as error:
        logger.warning('guardian_factor_risk_unavailable error=%s', type(error).__name__)
        return {}
    result, seen = {}, set()
    for row in rows:
        model = str(row.get('score_model_version') or '')
        symptom = next((kind for prefix, kind in MODEL_PREFIX.items() if model.startswith(prefix)), None)
        if not symptom:
            continue
        try:
            formula_id = int(row['formula_id'])
        except (TypeError, ValueError):
            continue
        key = (formula_id, symptom)
        if key in seen:
            continue
        seen.add(key)
        result.setdefault(formula_id, {})[symptom] = {
            'level': row.get('current_pool_risk_level'),
            'model_version': model,
            'reference_pool_version': row.get('reference_pool_version'),
            'main': _informative(_parse_tags(row.get('main_reason_tags'))),
            'support': _informative(_parse_tags(row.get('support_reason_tags'))),
            'fat_detail': _informative(_parse_tags(row.get('fat_detail_tags'))),
        }
    return result


def load_community_evidence():
    """All reviewable comment rows, indexed by problem text for in-memory matching."""
    sql = '''SELECT id,topic,subtopic,problem,pairs_json,evidence_status
        FROM kd_problem_guides
        WHERE evidence_status IS NOT NULL'''
    try:
        with pymysql.connect(**get_mysql_config(), cursorclass=pymysql.cursors.DictCursor) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                return cur.fetchall()
    except pymysql.MySQLError as error:
        logger.warning('guardian_factor_community_unavailable error=%s', type(error).__name__)
        return []


def _community_causes(pairs_json):
    """Only the cause side. Action text is medical advice and stays out of comparison."""
    try:
        pairs = json.loads(pairs_json) if isinstance(pairs_json, str) else (pairs_json or [])
    except (ValueError, TypeError):
        return []
    causes = []
    for pair in pairs if isinstance(pairs, list) else []:
        cause = str((pair or {}).get('cause') or '').strip()
        if cause and cause not in causes:
            causes.append(cause)
    return causes


def _community_for(factor, symptom, rows):
    keywords = COMMUNITY_KEYWORDS.get((factor, symptom)) or []
    if not keywords or not rows:
        return []
    hits = []
    for row in rows:
        problem = str(row.get('problem') or '')
        causes = _community_causes(row.get('pairs_json'))
        if not causes:
            continue
        haystack = problem + ' ' + ' '.join(causes)
        if not any(keyword in haystack for keyword in keywords):
            continue
        if not any(word in haystack for word in DIET_CONTEXT):
            continue
        if any(word in haystack for word in DRUG_CONTEXT):
            continue
        for cause in causes:
            if not any(keyword in (problem + ' ' + cause) for keyword in keywords):
                continue
            hits.append({'id': row['id'], 'topic': row.get('topic'), 'problem': problem[:60],
                         'cause': cause[:120], 'evidence_status': row.get('evidence_status') or '未知'})
            break
        if len(hits) >= MAX_COMMUNITY_PER_FACTOR:
            break
    return hits


def build_community_index(community_rows):
    """Pre-bucket by (factor, symptom): 10 lookups instead of one per comparison."""
    return {(factor, symptom): _community_for(factor, symptom, community_rows)
            for factor, symptom in COMMUNITY_KEYWORDS}


def build_profile(formula_id, risk_by_formula, positions, community_index):
    """Chain every symptom risk back to the factor tags that produced it."""
    formula_id = int(formula_id)
    symptoms = risk_by_formula.get(formula_id) or {}
    formula_positions = positions.get(formula_id) or {}
    chains = []
    factor_map = {key: {'structural': [], 'function': [], 'symptoms': []} for key in FACTOR_ORDER}
    for symptom, payload in symptoms.items():
        for tag in payload.get('main', []) + payload.get('support', []) + payload.get('fat_detail', []):
            factor = STRUCTURAL_TAGS.get(tag)
            side = 'structural'
            if not factor:
                factor = FUNCTION_TAGS.get(tag)
                side = 'function'
            if not factor:
                continue
            entry = factor_map.setdefault(factor, {'structural': [], 'function': [], 'symptoms': []})
            if tag not in entry[side]:
                entry[side].append(tag)
            if symptom not in entry['symptoms']:
                entry['symptoms'].append(symptom)
        grouped = {}
        for tag in payload.get('main', []) + payload.get('support', []) + payload.get('fat_detail', []):
            factor = STRUCTURAL_TAGS.get(tag) or FUNCTION_TAGS.get(tag)
            if not factor:
                continue
            slot = grouped.setdefault(factor, {'structural': [], 'function': []})
            key = 'structural' if tag in STRUCTURAL_TAGS else 'function'
            if tag not in slot[key]:
                slot[key].append(tag)
        for factor in FACTOR_ORDER:
            slot = grouped.get(factor)
            if not slot:
                continue
            mechanisms = [MECHANISM[tag] for tag in slot['structural'] + slot['function'] if tag in MECHANISM]
            chains.append({
                'symptom': symptom,
                'symptom_label': SYMPTOM_LABELS.get(symptom, symptom),
                'level': payload.get('level'),
                'factor': factor,
                'factor_label': FACTORS[factor]['label'],
                'percentile': formula_positions.get(FACTORS[factor]['dim']),
                'structural': slot['structural'],
                'function': slot['function'],
                'mechanism': mechanisms,
                'community': list((community_index or {}).get((factor, symptom)) or []),
            })
    order = {key: index for index, key in enumerate(FACTOR_ORDER)}
    chains.sort(key=lambda item: (order.get(item['factor'], 99), item['symptom']))
    return {
        'formula_id': formula_id,
        'module_version': MODULE_VERSION,
        'feature_version': DEFAULT_FEATURE_VERSION,
        'positions': {FACTORS[key]['dim']: formula_positions.get(FACTORS[key]['dim']) for key in FACTOR_ORDER},
        'factors': factor_map,
        'symptoms': {symptom: {'level': payload.get('level'),
                               'model_version': payload.get('model_version')}
                     for symptom, payload in symptoms.items()},
        'chains': chains,
    }


def _load_legacy_profiles(formula_ids):
    ids = sorted({int(i) for i in formula_ids if i})
    if not ids:
        return {}
    positions, pool_sizes = load_pool_positions()
    risk_by_formula = load_risk_tags(ids)
    community_index = build_community_index(load_community_evidence())
    return {formula_id: {**build_profile(formula_id, risk_by_formula, positions, community_index),
                         'pool_sizes': pool_sizes}
            for formula_id in ids}


def load_profiles(formula_ids):
    """Managed formulas only use the request's immutable published bundle."""
    from services.formula_calculation_service import published
    ids = sorted({int(i) for i in formula_ids if i})
    versions = published(ids)
    result = _load_legacy_profiles([i for i in ids if i not in versions])
    ready = {i: bundle for i, bundle in versions.items()
             if bundle.get('risks') and not bundle.get('invalidated')}
    if not ready:
        return result
    community = build_community_index(load_community_evidence())
    for formula_id, bundle in ready.items():
        risks = {r['kind']: {'level': r.get('relative_risk_level'),
                            'model_version': r.get('model_version'),
                            'reference_pool_version': r.get('reference_pool_version'),
                            'main': _informative(r.get('reason_tags') or []),
                            'support': _informative(r.get('support_reason_tags') or []),
                            'fat_detail': _informative(r.get('fat_detail_tags') or [])}
                 for r in bundle['risks']}
        # Mutable market percentiles are not part of this publication.
        result[formula_id] = {**build_profile(formula_id, {formula_id: risks}, {}, community),
                              'pool_sizes': {}, 'result_id': bundle['result_id'],
                              'calculation_version': bundle['calculation_version']}
    return result


def _tag_set(profile, factor, side=None):
    entry = (profile.get('factors') or {}).get(factor) or {}
    if side:
        return set(entry.get(side) or [])
    return set(entry.get('structural') or []) | set(entry.get('function') or [])


def compare_profiles(current, target):
    """Side-by-side factor diff. This is where two formulas stop looking identical."""
    result = {'status': 'unavailable', 'module_version': MODULE_VERSION, 'dimensions': [],
              'only_current': [], 'only_target': [], 'shared': [], 'differentiators': [],
              'note': '结构位次与风险标签都相对模型参考池，不是患病概率；社区条目未经核验，仅作观察。'}
    if not current or not target:
        result['note'] = '原粮或目标粮缺少结构数据或风险标签，暂不能做因子级对比。'
        return result
    if current.get('calculation_version') != target.get('calculation_version'):
        result['note'] = '两款粮的计算版本尚未对齐，更新完成后再比较。'
        return result
    if current.get('formula_id') and current.get('formula_id') == target.get('formula_id'):
        result['status'] = 'same_formula'
        result['note'] = '两款粮是同一个配方，没有可对比的差异。'
        return result
    for factor in FACTOR_ORDER:
        label = FACTORS[factor]['label']
        dim = FACTORS[factor]['dim']
        old_pct = (current.get('positions') or {}).get(dim)
        new_pct = (target.get('positions') or {}).get(dim)
        old_tags = _tag_set(current, factor)
        new_tags = _tag_set(target, factor)
        only_old = sorted(old_tags - new_tags)
        only_new = sorted(new_tags - old_tags)
        row = {'factor': factor, 'label': label, 'dimension': dim,
               'current_percentile': old_pct, 'target_percentile': new_pct,
               'current_tags': sorted(old_tags), 'target_tags': sorted(new_tags),
               'only_current_tags': only_old, 'only_target_tags': only_new,
               'shared_tags': sorted(old_tags & new_tags)}
        if old_pct is not None and new_pct is not None:
            row['gap'] = round(new_pct - old_pct, 4)
        result['dimensions'].append(row)
        if only_old or only_new:
            result['differentiators'].append(row)
        for tag in only_old:
            result['only_current'].append({'factor': factor, 'label': label, 'tag': tag})
        for tag in only_new:
            result['only_target'].append({'factor': factor, 'label': label, 'tag': tag})
        for tag in row['shared_tags']:
            result['shared'].append({'factor': factor, 'label': label, 'tag': tag})
    # Largest positional gap first: that is the most defensible difference.
    result['differentiators'].sort(key=lambda row: -abs(row.get('gap') or 0))
    available = any(row.get('current_percentile') is not None or row.get('target_percentile') is not None
                    for row in result['dimensions'])
    result['status'] = 'ready' if available else 'partial'
    if not result['differentiators'] and not result['shared']:
        result['note'] = ('两款粮的结构位次不同，但风险模型没有给出可区分的因子标签，'
                          '本次只呈现位次，不推断症状差异。')
    result['headline'] = build_headline(result)
    return result


def _pct_text(value):
    return '—' if value is None else '%d%%' % round(value * 100)


# "中高" must be tested before "中" and "高", so the order matters.
LEVEL_ORDER = ('中高', '中低', '高', '中', '低')


def _level_rank(level):
    text = str(level or '')
    for index, key in enumerate(LEVEL_ORDER):
        if key in text:
            return index
    return len(LEVEL_ORDER)


def _side_word(factor, delta):
    """Relative wording only: a 57% percentile is not low in absolute terms."""
    if delta > 0:
        return FACTORS[factor]['higher'] if factor in ('carb', 'protein', 'fat') else '支持更足'
    return '压力相对更低' if factor in ('carb', 'protein', 'fat') else '支持相对更少'


def build_headline(comparison):
    """One sentence naming the single most defensible difference."""
    rows = [row for row in comparison.get('dimensions') or []
            if row.get('current_percentile') is not None and row.get('target_percentile') is not None]
    if not rows:
        return ''
    biggest = max(rows, key=lambda row: abs(row.get('gap') or 0))
    tag_counts = {row['factor']: (len(row['only_current_tags']), len(row['only_target_tags'])) for row in rows}
    leader = max(tag_counts.items(), key=lambda item: max(item[1]), default=(None, (0, 0)))
    parts = []
    if biggest.get('gap') is not None and abs(biggest['gap']) >= 0.15:
        direction = '目标粮' + _side_word(biggest['factor'], biggest['gap'])
        parts.append('最大差别在%s：原粮 %s 分位，目标粮 %s 分位，%s' % (
            biggest['label'], _pct_text(biggest['current_percentile']),
            _pct_text(biggest['target_percentile']), direction))
    if leader[0] and max(leader[1]) >= 2:
        factor_label = FACTORS[leader[0]]['label']
        if leader[1][0] > leader[1][1]:
            parts.append('标签层面原粮在%s一侧多出 %d 项' % (factor_label, leader[1][0]))
        else:
            parts.append('标签层面目标粮在%s一侧多出 %d 项' % (factor_label, leader[1][1]))
    return '；'.join(parts) + '。' if parts else ''


def factor_comparison_text(comparison, current_profile=None, target_profile=None):
    """Compact rendering: the chat bubble cannot carry the full evidence tree.

    Full per-factor detail stays in the comparison dict for structured output.
    """
    if comparison.get('status') in ('unavailable', 'same_formula'):
        return comparison.get('note', '')
    lines = []
    if comparison.get('headline'):
        lines.append(comparison['headline'])
    dims = [row for row in comparison['dimensions']
            if row.get('current_percentile') is not None and row.get('target_percentile') is not None]
    headline_factor = None
    if dims:
        biggest = max(dims, key=lambda row: abs(row.get('gap') or 0))
        if abs(biggest.get('gap') or 0) >= .15:
            headline_factor = biggest['factor']
    # Skip the dimension the headline already named, otherwise it repeats.
    notable = [row for row in dims if abs(row.get('gap') or 0) >= .20 and row['factor'] != headline_factor][:3]
    if notable:
        lines.append('结构位次（相对全库，数字越大代表该项压力或支持越强）：')
        for row in notable:
            word = '两者接近' if abs(row['gap']) < .10 else '目标粮' + _side_word(row['factor'], row['gap'])
            lines.append('· %s：原粮 %s，目标粮 %s，%s' % (
                row['label'], _pct_text(row['current_percentile']),
                _pct_text(row['target_percentile']), word))
    if comparison['only_current'] or comparison['only_target']:
        lines.append('风险标签差异：')
        for key, title in (('only_current', '仅原粮'), ('only_target', '仅目标粮')):
            items = comparison[key]
            if not items:
                continue
            labels = '、'.join(dict.fromkeys(item['tag'] for item in items))
            extra = '' if len(items) <= 4 else '（共 %d 项）' % len(items)
            lines.append('· %s：%s%s' % (title, labels, extra))
        if comparison['shared']:
            lines.append('· 共有：' + '、'.join(dict.fromkeys(item['tag'] for item in comparison['shared'])))
    # Align by symptom: the point is seeing two formulas' factors under the same
    # symptom, not two unrelated symptoms side by side.
    symptom_rows = []
    for symptom in ('soft_stool', 'black_chin'):
        parts, mechanisms = [], []
        for title, profile in (('原粮', current_profile), ('目标粮', target_profile)):
            chains = sorted([chain for chain in (profile or {}).get('chains') or []
                             if chain['symptom'] == symptom],
                            key=lambda item: (_level_rank(item.get('level')),
                                              FACTOR_ORDER.index(item['factor'])
                                              if item['factor'] in FACTOR_ORDER else 99))
            if not chains:
                continue
            lead = chains[0]
            tags = '、'.join(dict.fromkeys(
                tag for chain in chains[:2] for tag in (chain['structural'] + chain['function'])[:2]))
            parts.append('%s：%s（%s）' % (title, tags or lead['factor_label'],
                                          lead.get('level') or '等级暂无'))
            if lead.get('mechanism'):
                mechanisms.append('%s侧%s' % (title, lead['mechanism'][0]))
        if parts:
            symptom_rows.append((SYMPTOM_LABELS.get(symptom, symptom), parts, mechanisms))
    if symptom_rows:
        lines.append('因子到症状（同一条症状下，两款粮被指出的因子不同）：')
        for label, parts, _ in symptom_rows:
            lines.append('· %s：%s' % (label, '；'.join(parts)))
        mechanism = next((row[2][0] for row in symptom_rows if row[2]), None)
        if mechanism:
            lines.append('· 机制说明：' + mechanism)
    community, seen_community = [], set()
    for profile in (current_profile, target_profile):
        for chain in (profile or {}).get('chains') or []:
            for item in chain.get('community') or []:
                if item['id'] in seen_community:
                    continue
                seen_community.add(item['id'])
                community.append((item, chain['factor_label']))
    if community:
        lines.append('社区经验（评论蒸馏，未经核验，只作观察）：')
        for item, factor_label in community[:2]:
            lines.append('· %s：「%s」（id=%s，%s）' % (
                factor_label, item['cause'], item['id'], item['evidence_status']))
    lines.append(comparison['note'])
    return '\n'.join(lines)
