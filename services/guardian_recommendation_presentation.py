"""Stable quality-first diversity and compact copy for the existing client."""
import re

from services.guardian_food_goals import GOALS

DIVERSITY_SCORE_WINDOW = 5.0  # Policy points on the existing 0..100 score, not a health threshold.


def alternatives_requested(message):
    return bool(re.fullmatch(r'(?:请|再|帮我|麻烦)?(?:换一组|换一批|看看其他候选|其他候选|其他推荐|别的推荐)(?:吧|看看)?[。！!]*', str(message or '').strip()))


def select_candidates(rows, limit=3):
    """Rows must already pass every eligibility gate and be sorted by quality."""
    remaining, seen = [], set()
    for row in rows:
        fid = row[0]['formula_id']
        if fid not in seen:
            remaining.append(row)
            seen.add(fid)
    selected, brands = [], set()
    while remaining and len(selected) < limit:
        best = remaining[0][1]['score']
        choice = next((i for i, row in enumerate(remaining)
                       if best - row[1]['score'] <= DIVERSITY_SCORE_WINDOW
                       and re.sub(r'\s+', '', str(row[0].get('brand') or '')).casefold() not in brands), 0)
        row = remaining.pop(choice)
        brands.add(re.sub(r'\s+', '', str(row[0].get('brand') or '')).casefold())
        selected.append(row)
    return selected


def compact_recommendation(result):
    """Preserve full evidence in analysis; old clients receive short candidate copy."""
    scenario = result.get('scenario') or {}
    goal = GOALS.get(result.get('goal'), {}).get('label') or '日常适配'
    scene = scenario.get('label', '').removesuffix('选粮')
    title = '本次按「' + '＋'.join(dict.fromkeys(filter(None, (scene, goal)))) + '」筛选'
    items = result.get('items') or []
    result['selection_summary'] = title
    result['display_intro'] = title + f'，本次找到{len(items)}款候选。'
    if scenario.get('notes'):
        result['display_intro'] += '\n' + '\n'.join(scenario['notes'])
    if result.get('missing_assessment_fields'):
        result['display_intro'] += '\n部分相关指标缺少资料，未计入评分，也未认定已通过对应风险筛选。'
    if not result.get('current_analysis') and not (result.get('reference') or {}).get('formula_id'):
        result['display_intro'] += '\n暂无可核验原粮，仅比较候选自身特点，不认定相对改善。'
    result['display_intro'] += '\n可以回复“选第一款”查看完整依据，或说“换一组”查看其他合格候选。'
    result['ranking_policy'] = {'version': 'quality-diversity-v1', 'score_window': DIVERSITY_SCORE_WINDOW,
                                'randomized': False, 'eligibility_relaxed': False}
    for item in items:
        reasons = list(dict.fromkeys(item.get('strengths') or []))[:2]
        caution = list(item.get('cautions') or [])
        if result.get('goal') == 'weight_loss':
            caution.insert(0, '本次按配方结构筛选，未核验能量密度；不据此认定减重效果。')
        caution.sort(key=lambda value: '未按年龄筛选' not in value)
        if not result.get('current_analysis') and not (result.get('reference') or {}).get('formula_id'):
            reasons = [r.replace('缺少原粮数据，未验证改善幅度', '无原粮数据，仅作候选自身特点参考') for r in reasons]
        item['recommendation_detail'] = {'strengths': item.get('strengths') or [],
                                         'cautions': item.get('cautions') or []}
        item['strengths'] = reasons
        item['cautions'] = list(dict.fromkeys(caution))[:2]
        stage = item.get('life_stage') or ''
        stage_label = {'adult': '成猫', 'kitten': '幼猫', 'senior': '老年猫',
                       'all': '全阶段', 'all_stages': '全阶段', 'all life stages': '全阶段'}.get(stage, stage)
        item['recommendation_card'] = {'version': 1, 'reasons': reasons, 'cautions': item['cautions'],
                                        'goal_label': goal, 'life_stage_label': stage_label,
                                        'life_stage': item.get('life_stage'),
                                        'price_range_per_jin': [item.get('price_min_per_jin'), item.get('price_max_per_jin')]}
    return result
