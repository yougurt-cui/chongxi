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
DISPLAY_NOTE = '这些是需要关注的表现，不代表这款粮会引起疾病。'
DISPLAY_TITLES = {
    ('肌肉与体态维持', 'advantage'): '肌肉维持',
    ('便便状态', 'advantage'): '排便健康',
    ('便便状态', 'attention'): '排便稳定',
    ('消化耐受', 'advantage'): '消化耐受',
    ('消化耐受', 'attention'): '软便、胀气',
}
DISPLAY_PRIORITY = {
    'advantage': {'肌肉与体态维持': 0, '便便状态': 1, '消化耐受': 2, '体重管理': 3},
    'attention': {'消化耐受': 0, '便便状态': 1, '体重管理': 2, '肌肉与体态维持': 3},
}
DISPLAY_FACTOR_SUMMARIES = {
    ('PROTEIN_QUALITY', 'high'): '蛋白质量评价较好，有利于为猫咪维持肌肉提供营养基础。',
    ('PROTEIN_QUALITY', 'low'): '蛋白质量支持偏弱，需要结合长期体态和肌肉状态观察。',
    ('PROTEIN_PRESSURE', 'high'): '蛋白结构压力偏高，消化敏感的猫咪需要留意适应情况。',
    ('PROTEIN_PRESSURE', 'low'): '蛋白结构压力较低，对日常消化耐受相对友好。',
    ('FAT_BURDEN', 'high'): '脂肪结构负担偏高，消化敏感或需要控制体重时应谨慎。',
    ('FAT_BURDEN', 'low'): '脂肪结构负担较低，对日常消化耐受相对友好。',
    ('FIBER_SUPPORT', 'high'): '纤维支持较好，有助于维持正常排便。',
    ('FIBER_SUPPORT', 'low'): '纤维支持偏弱，需要留意便便是否成形、排便是否规律。',
    ('CARB_BURDEN', 'high'): '碳水结构负担偏高，部分消化敏感的猫咪可能不太适应。',
    ('CARB_BURDEN', 'low'): '碳水结构负担较低，对日常消化耐受相对友好。',
    ('PREBIOTIC_SUPPORT', 'high'): '益生元支持较好，有助于维持肠道环境和排便稳定。',
    ('PREBIOTIC_SUPPORT', 'low'): '益生元支持偏少，需要留意便便是否经常忽软忽硬。',
}
DISPLAY_TOPIC_FACTOR_SUMMARIES = {
    ('消化耐受', 'PREBIOTIC_SUPPORT', 'low'): '益生元支持偏少，消化敏感的猫咪需要留意腹胀或排气增多。',
    ('便便状态', 'PREBIOTIC_SUPPORT', 'low'): '益生元支持偏少，需要留意便便是否经常忽软忽硬。',
}


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
    # Explicit symptom topics remain cautions. For structural topics, use the
    # direction of the six-dimension signal so a favourable score cannot be
    # displayed under "需要留心" merely because the knowledge row also carries
    # an observation reminder.
    if row['health_focus'] in {'软便', '便秘', '胀气'}:
        return 'attention'
    direction = FACTORS[dimension][3]
    favourable = (
        direction == 'support' and row['factor_status'] == 'high'
        or direction == 'burden' and row['factor_status'] == 'low'
    )
    if favourable:
        return 'advantage'
    if row.get('relation_type') in TAGS:
        return row['relation_type']
    return 'attention'


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
            # The same health topic can contain both a structural strength and a
            # caveat (for example high fibre plus low prebiotic support).  Keep
            # them separate so the product card never hides an advantage inside
            # an attention group.
            group_key = (title, kind)
            group = groups.setdefault(group_key, {'title': title, 'kind': kind, 'evidence': [],
                                             'mechanisms': [], 'observations': [], 'sources': []})
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
    for group in ordered:
        group['tag'] = TAGS[group['kind']]
        group['evidence_text'] = '；'.join(f'{e["label"]}（{e["score"]:g}/{e["scale_max"]:g}）' for e in group['evidence'])
        group['mechanism_text'] = '\n'.join(group['mechanisms'])
        group['observation_text'] = '\n'.join(group['observations'])
    result['concerns'] = ordered
    result['presentation'] = build_presentation(ordered)
    result['status'] = ('partial' if result['missing_dimensions'] or result['unmapped_dimensions'] else 'ready') if ordered else 'no_match'
    result['message'] = ('以下关注点来自产品画像与健康知识映射。' if ordered else
                         '当前没有命中可用的高低档健康映射；中间档暂不解读，不能据此判断没有健康风险。')
    return result


def build_presentation(concerns):
    """Create the compact product-card copy without exposing portrait scores."""
    advantages = []
    attentions = []
    display_concerns = sorted(
        concerns,
        key=lambda item: (
            PRIORITY.get(item.get('kind'), 9),
            DISPLAY_PRIORITY.get(item.get('kind'), {}).get(item.get('title'), 99),
            -len(item.get('evidence') or []),
        ),
    )
    for concern in display_concerns:
        kind = concern.get('kind')
        if kind not in {'advantage', 'attention'}:
            continue
        item = {
            'title': DISPLAY_TITLES.get((concern.get('title'), kind), concern.get('title')),
            'summary': _display_summary(concern),
            'observation': concern.get('observation_text') or '',
            'source_title': concern.get('title'),
        }
        target = advantages if kind == 'advantage' else attentions
        if len(target) < 2:
            target.append(item)
    return {
        'advantages': advantages,
        'attentions': attentions,
        'note': DISPLAY_NOTE,
    }


def _display_summary(concern):
    summaries = []
    for evidence in concern.get('evidence') or []:
        text = DISPLAY_TOPIC_FACTOR_SUMMARIES.get((
            concern.get('title'), evidence.get('factor_code'), evidence.get('level')
        )) or DISPLAY_FACTOR_SUMMARIES.get((evidence.get('factor_code'), evidence.get('level')))
        if text and text not in summaries:
            summaries.append(text)
    return ''.join(summaries) or concern.get('mechanism_text') or ''


def report_text(label, report):
    """Complete plain-text fallback for older clients and conversation history."""
    lines = [f'**{label}**']
    presentation = report.get('presentation') or build_presentation(report.get('concerns') or [])
    advantages = presentation.get('advantages') or []
    attentions = presentation.get('attentions') or []
    if not advantages and not attentions:
        lines.append(report['message'])
    if advantages:
        lines.append('✅ **1. 好在哪？**')
        lines.extend(f'**{item["title"]}：** {item["summary"]}' for item in advantages)
    if attentions:
        lines.append('⚠️ **2. 哪里需要留心？**')
        lines.extend(f'**{item["title"]}：** {item["summary"]}' for item in attentions)
        lines.append(presentation.get('note') or DISPLAY_NOTE)
    if report['missing_dimensions']:
        lines.append('资料不足：' + '、'.join(report['missing_dimensions']))
    if report['unmapped_dimensions']:
        lines.append('暂缺对应知识：' + '、'.join(report['unmapped_dimensions']))
    return '\n\n'.join(lines)
