"""Read-only, formula/version keyed five-dimensional evidence and comparisons."""
import json
import logging
from math import isfinite

import pymysql
from app_config import get_feature_mysql_config
from services.guardian_food_scenario import DIMENSIONS, score
from services.product_portrait_summary_service import PORTRAIT_VERSION, DEFAULT_FEATURE_VERSION

logger = logging.getLogger(__name__)
PORTRAIT_DIMENSIONS = {
    'protein_quality_score': ('蛋白质量', 1.0, 'protein_quality'),
    **DIMENSIONS,
}


def _portrait_score(value, field, maximum):
    if field in DIMENSIONS:
        return score(value, field)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number / maximum if isfinite(number) and 0 <= number <= maximum else None


def load_five_features(formula_ids):
    ids = sorted({int(i) for i in formula_ids if i})
    if not ids:
        return {}
    try:
        with pymysql.connect(**get_feature_mysql_config(), cursorclass=pymysql.cursors.DictCursor) as conn:
            with conn.cursor() as cur:
                marks = ','.join(['%s'] * len(ids))
                cur.execute(f'''SELECT s.id AS source_feature_id,s.formula_id,s.feature_version,
                    s.protein_score,s.fat_score,s.fiber_score,s.carb_score,s.prebiotic_score
                    FROM sku_feature_input s
                    JOIN csv_labeling.catfood_standard_formula f ON f.formula_id=s.formula_id
                        AND f.status='active' AND f.is_current=1
                    WHERE s.feature_version=%s AND s.formula_id IN ({marks})
                    AND s.id=(SELECT MAX(v.id) FROM sku_feature_input v
                              WHERE v.formula_id=s.formula_id AND v.feature_version=s.feature_version)''',
                            [DEFAULT_FEATURE_VERSION, *ids])
                rows = cur.fetchall()
        return {int(r['formula_id']): {**r, **{k: (float(r[k]) if score(r.get(k), k) is not None else None)
                                                       for k in DIMENSIONS}} for r in rows}
    except (pymysql.MySQLError, ValueError, TypeError) as error:
        logger.warning('guardian_five_features_unavailable error=%s', type(error).__name__)
        return {}


def load_product_portraits(formula_ids):
    """Never rebuild in chat. Stale, incompatible or invalid rows are unavailable."""
    ids = sorted({int(i) for i in formula_ids if i})
    if not ids:
        return {}
    try:
        with pymysql.connect(**get_feature_mysql_config(), cursorclass=pymysql.cursors.DictCursor) as conn:
            with conn.cursor() as cur:
                marks = ','.join(['%s'] * len(ids))
                same_scores = ' AND '.join(f'p.{key} <=> s.{key}' for key in DIMENSIONS)
                cur.execute(f'''SELECT p.formula_id,p.portrait_json,p.source_feature_id,p.source_data_hash
                    FROM catfood_formula_portrait_summary p
                    JOIN csv_labeling.catfood_standard_formula f ON f.formula_id=p.formula_id
                        AND f.status='active' AND f.is_current=1
                    JOIN sku_feature_input s ON s.id=p.source_feature_id AND s.formula_id=p.formula_id
                        AND BINARY s.feature_version=BINARY p.feature_version
                    LEFT JOIN catfood_protein_fat_fiber_score_wide w
                        ON w.formula_id=p.formula_id
                    WHERE p.status='active' AND p.feature_version=%s AND p.portrait_version=%s
                        AND p.formula_id IN ({marks}) AND {same_scores}
                        AND p.protein_quality_score <=> w.protein_quality_score
                        AND s.id=(SELECT MAX(v.id) FROM sku_feature_input v
                                  WHERE v.formula_id=s.formula_id AND v.feature_version=s.feature_version)''',
                            [DEFAULT_FEATURE_VERSION, PORTRAIT_VERSION, *ids])
                rows = cur.fetchall()
        result = {}
        for row in rows:
            try:
                portrait = row['portrait_json']
                portrait = json.loads(portrait) if isinstance(portrait, str) else portrait
                if not isinstance(portrait, dict) or portrait.get('formula_id') != int(row['formula_id']):
                    continue
                if (portrait.get('feature_version') != DEFAULT_FEATURE_VERSION or portrait.get('portrait_version') != PORTRAIT_VERSION
                        or portrait.get('source_feature_id') != row['source_feature_id']
                        or portrait.get('source_data_hash') != row['source_data_hash']):
                    continue
                dims = portrait.get('dimensions')
                if not isinstance(dims, dict):
                    continue
                valid = {}
                for field, (label, maximum, name) in PORTRAIT_DIMENSIONS.items():
                    item = dims.get(name) or {}
                    if (not isinstance(item, dict) or item.get('scale_max') != maximum
                            or _portrait_score(item.get('score'), field, maximum) is None):
                        continue
                    valid[name] = {**item, 'score': float(item['score'])}
                result[int(row['formula_id'])] = {**portrait, 'dimensions': valid}
            except (ValueError, TypeError, KeyError):
                continue
        return result
    except pymysql.MySQLError as error:
        logger.warning('guardian_portraits_unavailable error=%s', type(error).__name__)
        return {}


def compare_portraits(current, target):
    result = {'status': 'unavailable', 'common': [], 'complements': [], 'shortcomings': [],
              'missing_dimensions': [], 'feature_version': DEFAULT_FEATURE_VERSION,
              'portrait_version': PORTRAIT_VERSION,
              'note': '画像比较描述配方特点，不代表实际健康效果；互补不表示建议混喂。'}
    if not current or not target:
        result['note'] = '原粮或目标粮缺少有效的当前版本画像，暂不能判断共同特点、互补关系和短板。'
        return result
    if any(p.get('feature_version') != DEFAULT_FEATURE_VERSION or p.get('portrait_version') != PORTRAIT_VERSION
           for p in (current, target)):
        result['note'] = '画像版本不一致，暂不比较。'
        return result
    result.update(current_formula_id=current['formula_id'], target_formula_id=target['formula_id'])
    for field, (label, maximum, name) in PORTRAIT_DIMENSIONS.items():
        a, b = (current.get('dimensions') or {}).get(name, {}), (target.get('dimensions') or {}).get(name, {})
        old = _portrait_score(a.get('score'), field, maximum)
        new = _portrait_score(b.get('score'), field, maximum)
        if old is None or new is None or a.get('scale_max') != maximum or b.get('scale_max') != maximum:
            result['missing_dimensions'].append(label)
            continue
        evidence = {'dimension': name, 'current_score': a['score'], 'target_score': b['score'], 'scale_max': maximum}
        if abs(old - new) <= .10 + 1e-9:
            result['common'].append({**evidence, 'text': f'两款粮的{label}结构接近（{a["score"]:g} / {b["score"]:g}）'})
        burden = field in {'protein_score', 'fat_score', 'carb_score'}
        if burden and old - new > .10:
            result['complements'].append({**evidence, 'text': f'目标粮的{label}比原粮更低，可作为这一维度的调整方向'})
        elif not burden and new - old > .10 and new <= .75:
            result['complements'].append({**evidence, 'text': f'目标粮的{label}更高，可补足原粮这一维度支持较少的特点，仍需结合当前场景'})
        if burden and (new > .65 or new - old > .10):
            result['shortcomings'].append({**evidence, 'text': f'目标粮的{label}' + ('比原粮更高' if new > old else '仍偏高')})
        elif not burden and (new <= .25 or new > .75):
            result['shortcomings'].append({**evidence, 'text': f'目标粮的{label}' + ('偏低' if new <= .25 else '较高，不能视为越高越适合，恢复期需核对变化幅度')})
    result['status'] = 'partial' if result['missing_dimensions'] else 'ready'
    return result


def portrait_comparison_text(comparison):
    if comparison['status'] == 'unavailable':
        return comparison['note']
    lines = []
    for key, title in [('common', '共同特点'), ('complements', '可补足的方面'), ('shortcomings', '潜在短板')]:
        texts = [item['text'] for item in comparison[key]]
        lines.append(title + '：' + ('；'.join(texts) if texts else '现有画像未发现明确证据'))
    if comparison['missing_dimensions']:
        lines.append('画像缺项：' + '、'.join(comparison['missing_dimensions']))
    return '\n'.join(lines + [comparison['note']])
