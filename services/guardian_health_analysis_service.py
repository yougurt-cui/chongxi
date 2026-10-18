"""Traceable nutrition interpretation from existing portraits and factor mappings.

The imported active v1 mappings are product-owned content. Their pending review
metadata is retained as provenance, never presented as clinical validation.
No model supplies mechanisms, and intermediate portrait levels never match.
"""
from __future__ import annotations

import logging
from math import isfinite

import pymysql
from app_config import get_feature_mysql_config
from services.product_portrait_summary_service import DEFAULT_FEATURE_VERSION, PORTRAIT_VERSION

logger = logging.getLogger(__name__)
KNOWLEDGE_VERSION = 'v1'
RULE_VERSION = 'food-health-v1'
TABLE = 'catfood_nutrition_factor_health_mapping'
FACTORS = {
    'protein_quality': ('PROTEIN_QUALITY', '蛋白质量', 1, 'support'),
    'protein': ('PROTEIN_PRESSURE', '蛋白压力', 1, 'burden'),
    'fat': ('FAT_BURDEN', '脂肪负担', 1, 'burden'),
    'fiber': ('FIBER_SUPPORT', '纤维支持', 5, 'support'),
    'carbohydrate': ('CARB_BURDEN', '碳水负担', 5, 'burden'),
    'prebiotic': ('PREBIOTIC_SUPPORT', '益生元支持', 5, 'support'),
}
FOCUS_GROUPS = {
    '软便': '便便状态', '便秘': '便便状态', '便便稳定': '便便状态',
    '肌肉维持': '肌肉与体态维持', '增重支持': '肌肉与体态维持',
    '胀气': '消化耐受', '肠道舒适度': '消化耐受',
    '减重管理': '体重管理',
}
TAGS = {'attention': '重点观察', 'advantage': '配方优势参考', 'routine': '常规观察'}
PRIORITY = {'attention': 0, 'advantage': 1, 'routine': 2}
NOTE = '画像分数描述配方结构，不代表营养含量、患病概率或实际健康效果；请结合实际吃粮表现观察。'


def load_health_mappings():
    """One pinned knowledge version; a missing table is an explicit degraded state."""
    try:
        with pymysql.connect(**get_feature_mysql_config(), cursorclass=pymysql.cursors.DictCursor) as conn:
            with conn.cursor() as cur:
                cur.execute(f'''SELECT mapping_code,factor_code,factor_name,factor_status,
                    health_focus,health_mechanism,user_observation,relation_type,
                    knowledge_version,review_status,status
                    FROM {TABLE} WHERE knowledge_version=%s AND status='active'
                    AND review_status IN ('pending','approved') ORDER BY mapping_code''', (KNOWLEDGE_VERSION,))
                return {'status': 'ready', 'rows': cur.fetchall(), 'knowledge_version': KNOWLEDGE_VERSION}
    except pymysql.MySQLError as error:
        logger.warning('guardian_health_mapping_unavailable error=%s', type(error).__name__)
        return {'status': 'unavailable', 'rows': [], 'knowledge_version': KNOWLEDGE_VERSION}


def _kind(row, dimension):
    # A support score can also carry an observation caveat (e.g. fermentation).
    if row.get('relation_type') in TAGS:
        return row['relation_type']
    if row['health_focus'] in {'软便', '便秘', '胀气'}:
        return 'attention'
    direction = FACTORS[dimension][3]
    if (direction == 'burden' and row['factor_status'] == 'high'
            or direction == 'support' and row['factor_status'] == 'low'):
        return 'attention'
    return 'advantage' if direction == 'support' else 'routine'


def analyse_portrait(portrait, knowledge, *, health_state=None):
    result = {'status': 'unavailable', 'concerns': [], 'missing_dimensions': [],
              'skipped_dimensions': [], 'unmapped_dimensions': [],
              'knowledge_version': KNOWLEDGE_VERSION, 'rule_version': RULE_VERSION, 'note': NOTE}
    if not portrait or portrait.get('portrait_version') != PORTRAIT_VERSION or portrait.get('feature_version') != DEFAULT_FEATURE_VERSION:
        return {**result, 'reason': 'portrait_unavailable', 'message': '缺少有效的当前版本产品画像，暂不能生成营养健康解读。'}
    result.update(formula_id=portrait['formula_id'], portrait_version=portrait['portrait_version'],
                  source_data_hash=portrait.get('source_data_hash'), source_feature_id=portrait.get('source_feature_id'))
    if knowledge.get('status') != 'ready':
        return {**result, 'reason': 'knowledge_unavailable', 'message': '健康解读知识暂时无法读取，可稍后重试。'}
    groups = {}
    for dimension, (code, label, maximum, _) in FACTORS.items():
        item = (portrait.get('dimensions') or {}).get(dimension) or {}
        try:
            value = float(item['score'])
            valid = isfinite(value) and 0 <= value <= maximum and item.get('scale_max') == maximum
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid or item.get('level') == 'missing':
            result['missing_dimensions'].append(label)
            continue
        level = item.get('level')
        if level not in {'high', 'low'}:
            result['skipped_dimensions'].append(label)
            continue
        rows = [r for r in knowledge.get('rows') or []
                if r.get('factor_code') == code and r.get('factor_status') == level
                and r.get('knowledge_version') == KNOWLEDGE_VERSION and r.get('status') == 'active'
                and r.get('review_status') in {'pending', 'approved'}
                and all(r.get(k) for k in ('mapping_code', 'health_focus', 'health_mechanism', 'user_observation'))]
        if not rows:
            result['unmapped_dimensions'].append(label)
        for row in rows:
            title = FOCUS_GROUPS.get(row['health_focus'], row['health_focus'])
            kind = _kind(row, dimension)
            group = groups.setdefault(title, {'title': title, 'kind': kind, 'evidence': [],
                                             'mechanisms': [], 'observations': [], 'sources': []})
            if PRIORITY[kind] < PRIORITY[group['kind']]:
                group['kind'] = kind
            evidence = {'dimension': dimension, 'factor_code': code, 'label': item.get('label') or label,
                        'score': value, 'scale_max': maximum, 'level': level}
            if evidence not in group['evidence']:
                group['evidence'].append(evidence)
            for key, text in (('mechanisms', row['health_mechanism']), ('observations', row['user_observation'])):
                if text not in group[key]:
                    group[key].append(text)
            group['sources'].append({k: row.get(k) for k in ('mapping_code', 'factor_code', 'factor_status',
                'health_focus', 'knowledge_version', 'review_status')})
    focus = {'soft_stool': '便便状态', 'vomiting': '消化耐受'}.get(health_state)
    ordered = sorted(groups.values(), key=lambda g: (g['title'] != focus, PRIORITY[g['kind']], -len(g['evidence']), g['title']))
    for group in ordered[:3]:
        group['tag'] = TAGS[group['kind']]
        group['evidence_text'] = '；'.join(f'{e["label"]}（{e["score"]:g}/{e["scale_max"]:g}）' for e in group['evidence'])
        group['mechanism_text'] = '\n'.join(group['mechanisms'])
        group['observation_text'] = '\n'.join(group['observations'])
    result['concerns'] = ordered[:3]
    result['status'] = ('partial' if result['missing_dimensions'] or result['unmapped_dimensions'] else 'ready') if ordered else 'no_match'
    result['message'] = ('以下关注点来自产品画像与健康知识映射。' if ordered else
                         '当前没有命中可用的高低档健康映射；中间档暂不解读，不能据此判断没有健康风险。')
    return result


def report_text(label, report):
    """Complete plain-text fallback for older clients and conversation history."""
    lines = [f'**{label} · 营养健康解读**']
    if not report['concerns']:
        lines.append(report['message'])
    for concern in report['concerns']:
        lines.extend([f'**{concern["title"]}（{concern["tag"]}）**', concern['evidence_text'],
                      concern['mechanism_text'], '❗ ' + concern['observation_text']])
    if report['missing_dimensions']:
        lines.append('资料不足：' + '、'.join(report['missing_dimensions']))
    if report['unmapped_dimensions']:
        lines.append('暂缺对应知识：' + '、'.join(report['unmapped_dimensions']))
    if report['skipped_dimensions']:
        lines.append('中间档暂不解读：' + '、'.join(report['skipped_dimensions']))
    lines.append(NOTE)
    return '\n\n'.join(lines)
