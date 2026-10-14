"""Versioned business rules for requirement 16; scores are not clinical values.

All tolerances are fractions of a dimension's declared scale. They are initial
retrieval policy, never feeding amounts or promises of a treatment effect.
"""
from copy import deepcopy
import math
import re

VERSION = 'guardian-scenario-v1'
DIMENSIONS = {
    'protein_score': ('蛋白压力', 1.0, 'protein'),
    'fat_score': ('脂肪负担', 1.0, 'fat'),
    'fiber_score': ('纤维支持', 5.0, 'fiber'),
    'carb_score': ('碳水负担', 5.0, 'carbohydrate'),
    'prebiotic_score': ('益生元支持', 5.0, 'prebiotic'),
}
STATES = {'none': '无明显问题', 'soft_stool': '软便', 'vomiting': '呕吐',
          'black_chin': '黑下巴', 'gi_history': '肠胃炎病史', 'unknown': '暂不清楚'}
STAGES = {'active': '正在发生', 'recovering': '恢复中', 'recovered': '已恢复／目前稳定',
          'none': '无相关问题', 'unknown': '暂不清楚'}

# keep: maximum change; improve: (direction, maximum change or None).
RULES = {
    ('soft_stool', 'active'): dict(keep=['protein_score', 'carb_score'],
        improve={'fat_score': ('down', None), 'prebiotic_score': ('balance', None)},
        bands={'fiber_score': (.35, .75), 'prebiotic_score': (.25, .65)}),
    ('soft_stool', 'recovering'): dict(keep=['protein_score', 'carb_score', 'fiber_score'],
        tolerance=.10, improve={'fat_score': ('down', .10), 'prebiotic_score': ('up', .10)}),
    ('soft_stool', 'recovered'): dict(keep=['protein_score', 'carb_score', 'fiber_score'], improve={}),
    ('vomiting', 'active'): dict(blocked=True, keep=[], improve={}),
    ('vomiting', 'recovering'): dict(keep=['protein_score', 'carb_score'],
        improve={'fat_score': ('down', None)}, max_rise={'fiber_score': .10, 'prebiotic_score': .10}),
    ('vomiting', 'recovered'): dict(keep=['protein_score', 'carb_score'], improve={}),
    ('black_chin', 'active'): dict(keep=['protein_score', 'fiber_score', 'carb_score'],
        improve={'fat_score': ('down', None)}),
    ('black_chin', 'recovering'): dict(keep=['protein_score', 'fiber_score', 'carb_score', 'prebiotic_score'],
        tolerance=.10, improve={'fat_score': ('down', .10)}),
    ('black_chin', 'recovered'): dict(keep=['protein_score', 'fiber_score', 'carb_score'], improve={}),
    ('gi_history', 'recovered'): dict(keep=['protein_score', 'carb_score', 'fiber_score'],
        improve={'fat_score': ('down', None), 'prebiotic_score': ('up', .15)}),
}
GOAL_RULES = {
    'weight_loss': dict(keep=[], improve={'fat_score': ('down', None), 'carb_score': ('down', None),
        'fiber_score': ('balance', None)}, bands={'protein_score': (0, .65), 'fiber_score': (.35, .75)}),
    'weight_gain': dict(keep=['protein_score', 'carb_score', 'fiber_score', 'prebiotic_score'],
        improve={}, note='五维结构分不代表热量，增重所需能量和适口性仍需单独核验。'),
    'soft_stool': dict(keep=[], improve={'fat_score': ('down', None), 'fiber_score': ('balance', None),
        'prebiotic_score': ('balance', None)}, bands={'fiber_score': (.35, .75), 'prebiotic_score': (.25, .65)}),
    'black_chin': dict(keep=[], improve={'fat_score': ('down', None)}),
    'coat_health': dict(keep=[], improve={}, note='五维画像不足以证明美毛效果，脂肪酸等专项资料仍待核验。'),
    'palatability': dict(keep=[], improve={}, note='五维画像不衡量适口性，需要结合实际进食反馈。'),
    'tear_stain': dict(keep=[], improve={}, note='五维画像不足以证明改善泪痕的效果。'),
    'general': dict(keep=[], improve={}),
}


def score(value, key):
    try:
        number = float(value)
        scale = DIMENSIONS[key][1]
        return number / scale if math.isfinite(number) and 0 <= number <= scale else None
    except (ValueError, TypeError):
        return None


def extract_health(message, slots, pending=None):
    """Only explicit assertions; goals, hypothetical questions and negations aren't symptoms."""
    text = str(message or '').strip()
    result = {}
    if not text:
        return result
    if pending in {'food_health_state', 'food_health_stage'} and re.search(r'不知道|不清楚|不确定', text):
        return {pending: 'unknown'}
    clauses = re.split(r'[，,。；;！!]|但是|不过|但', text)
    mentions = []
    for clause in clauses:
        if re.search(r'以前|之前|过去|曾经', clause) and re.search(r'吃|喂', clause) and not re.search(r'现在|今天|目前|最近', clause):
            continue
        if re.search(r'如果|假如|假设|是否|会不会|有没有|吗|[？?]|希望|想改善|预防|可能|好像|不确定|不知道', clause):
            continue
        for kind, pattern in [('vomiting', r'呕吐|吐了|还在吐'), ('soft_stool', r'软便|拉稀|腹泻'),
                              ('black_chin', r'黑下巴'), ('gi_history', r'肠胃炎|胃肠炎')]:
            found = re.search(pattern, clause)
            if not found or re.search(r'(?:没有|没|无|不|未)(?:再|出现|发生|过)?$', clause[:found.start()]):
                continue
            stage = ('recovering' if re.search(r'恢复中|恢复期|好转|缓解|还没好|未恢复|还没恢复', clause) else
                     'recovered' if re.search(r'已恢复|已经恢复|恢复了|好了|稳定|已痊愈', clause) else None)
            if not stage and re.search(r'正在|现在|目前|今天|最近|一直|还在|又|发生|出现|吐了', clause):
                stage = 'active'
            # A past episode says nothing about its present stage.
            mentions.append((kind, stage))
    if mentions:
        active_vomit = next((m for m in mentions if m == ('vomiting', 'active')), None)
        if active_vomit:
            mentions = [active_vomit]
        if len({m[0] for m in mentions}) > 1:
            return {'food_health_state': 'unknown', 'food_health_stage': 'unknown',
                    '_food_health_conflict': True}
        kind, stage = mentions[-1]
        if not stage and not re.search(r'如果|是否|会不会|[？?]|可能|好像|不确定|不知道', text):
            if re.search(r'恢复中|恢复期|好转|还没好', text):
                stage = 'recovering'
            elif re.search(r'已恢复|已经恢复|恢复了|已痊愈|目前稳定|现在好了|已经好了', text):
                stage = 'recovered'
        result['food_health_state'] = kind
        # A repeated condition with a newly unspecified phase must not reuse an old recovery claim.
        result['food_health_stage'] = stage
    elif re.search(r'无明显问题|没有明显问题|目前健康|身体正常|没有不适|没有相关问题|无症状|没有症状', text):
        result.update(food_health_state='none', food_health_stage='none')
    elif pending == 'food_health_state':
        if text in {'正常', '健康', '没有', '无'}:
            result.update(food_health_state='none', food_health_stage='none')
    if not mentions and slots.get('food_health_state') not in (None, 'none', 'unknown') and not re.search(r'如果|是否|会不会|[？?]|可能|好像|不确定|不知道', text):
        if re.search(r'恢复中|恢复期|好转|还没好', text):
            result['food_health_stage'] = 'recovering'
        elif re.search(r'已恢复|已经恢复|恢复了|好了|已痊愈|目前稳定', text):
            result['food_health_stage'] = 'recovered'
        elif pending == 'food_health_stage' and re.search(r'正在发生|还在|正在|又开始', text):
            result['food_health_stage'] = 'active'
    if result and '_food_health_conflict' not in result:
        result['_food_health_conflict'] = False
    return result


def build_scenario(slots):
    health = slots.get('food_health_state')
    stage = slots.get('food_health_stage')
    goal = slots.get('food_goal') or 'general'
    if health not in STATES:
        return {'status': 'needs_state', 'version': VERSION}
    if health not in {'none', 'unknown'} and stage not in STAGES:
        return {'status': 'needs_stage', 'version': VERSION}
    if health == 'gi_history' and stage in {'active', 'recovering'}:
        return {'status': 'limited', 'version': VERSION,
                'note': '肠胃炎尚未恢复稳定，病史稳定期的选粮规则暂不适用，需要结合当前诊疗和饮食要求确认。'}
    rule = deepcopy(RULES.get((health, stage), {}))
    if health not in {'none', 'unknown'} and not rule:
        return {'status': 'needs_stage', 'version': VERSION} if stage != 'unknown' else {
            'status': 'limited', 'version': VERSION, 'note': '当前阶段不明确，先确认是在发生、恢复中还是已经稳定，再做针对性推荐。'}
    if health == 'unknown':
        return {'status': 'limited', 'version': VERSION, 'note': '身体状态尚未确认，暂不能确定需要保持和改善的方向。可先比较具体产品资料。'}
    goal_rule = deepcopy(GOAL_RULES.get(goal, GOAL_RULES['general']))
    if health == 'none':
        rule = goal_rule
    elif stage == 'recovered' and goal not in {'general', 'soft_stool', 'black_chin'}:
        # Preserve the established structure; a conflicting long-term objective is deferred.
        for key, value in goal_rule.get('improve', {}).items():
            if key not in rule.get('keep', []):
                rule.setdefault('improve', {})[key] = value
        rule.setdefault('bands', {}).update({k: v for k, v in goal_rule.get('bands', {}).items()
                                             if k not in rule.get('keep', [])})
    notes = [goal_rule['note']] if goal_rule.get('note') else []
    if health != 'none' and stage in {'active', 'recovering'} and goal not in {'soft_stool', 'black_chin', 'general'}:
        notes.append('当前优先保持已耐受结构并按恢复阶段调整；长期目标保留，本轮不叠加与稳定要求冲突的变化。')
    specialty_unverified = not rule.get('improve') and goal in {'weight_gain', 'coat_health', 'palatability', 'tear_stain'}
    return {'version': VERSION, 'status': 'blocked' if rule.get('blocked') else 'limited' if specialty_unverified else 'ready',
            'health_state': health, 'stage': stage, 'goal': goal,
            'label': STATES[health] + ('／' + STAGES[stage] if health != 'none' else '') + '选粮',
            'keep': rule.get('keep', []), 'tolerance': rule.get('tolerance', .15),
            'improve': rule.get('improve', {}), 'bands': rule.get('bands', {}),
            'max_rise': rule.get('max_rise', {}), 'notes': notes,
            'note': ('目前有呕吐，先核对风险和原因，暂不生成普通换粮推荐。' if rule.get('blocked') else
                     goal_rule['note'] + '目前不能按这个目标排出推荐，可先比较指定产品的已有画像。' if specialty_unverified else '')}


def assess_candidate(row, baseline, scenario):
    """Apply preservation gates before ranking improvement; never fill missing scores."""
    values = {k: score(row.get(k), k) for k in DIMENSIONS}
    before = {k: score((baseline or {}).get(k), k) for k in DIMENSIONS}
    if any(v is None for v in values.values()):
        return {'eligible': False, 'reason': 'five_dimensions_missing'}
    preserved, improved, failed, gains = [], [], [], []
    for key in scenario['keep']:
        if before[key] is None or abs(values[key] - before[key]) > scenario['tolerance'] + 1e-9:
            failed.append(key)
        else:
            preserved.append(DIMENSIONS[key][0] + '结构接近已确认耐受的参考粮')
    for key, bounds in scenario['bands'].items():
        if not bounds[0] <= values[key] <= bounds[1]:
            failed.append(key)
    for key, ceiling in scenario['max_rise'].items():
        if before[key] is None or values[key] - before[key] > ceiling + 1e-9:
            failed.append(key)
    for key, (direction, ceiling) in scenario['improve'].items():
        value, old = values[key], before[key]
        label = DIMENSIONS[key][0]
        if direction == 'balance':
            low, high = scenario['bands'].get(key, (.25, .65))
            distance = lambda v: max(low - v, 0, v - high)
            gain = distance(old) - distance(value) if old is not None else 0
            gains.append(max(0, gain))
            if gain > .005:
                improved.append(label + '由参考区间外调整至区间内')
            else:
                preserved.append(label + '处于本场景的参考区间，未据此认定改善')
            continue
        if old is None:
            # An absolute ranking is possible, but no relative improvement claim is made.
            gains.append(1 - value if direction == 'down' else value)
            improved.append(label + ('参考分较低' if direction == 'down' else '参考分较高') + '，缺少原粮数据，未验证改善幅度')
            continue
        gain = old - value if direction == 'down' else value - old
        gains.append(max(0, gain))
        if gain < -.000001 or (ceiling is not None and gain > ceiling + 1e-9):
            failed.append(key)
        if gain > .005:
            improved.append(f"{label}参考分{float(baseline[key]):g}→{float(row[key]):g}" + ('，变化在小幅调整范围内' if ceiling else ''))
    # No unchanged clone can satisfy an explicitly directional improvement request.
    if scenario['improve'] and not any(gain > .005 for gain in gains):
        failed.append('no_improvement')
    return {'eligible': not failed, 'failed_dimensions': failed, 'keep_reasons': preserved,
            'improvement_reasons': improved, 'score': round(sum(gains) / max(1, len(gains)) * 100, 2),
            'note': '五维结构参考分，不是营养含量或健康效果；保持项只验证这些指标接近。'}


def scenario_question(scenario):
    if scenario['status'] == 'needs_state':
        return 'food_health_state', '它目前有什么身体问题或相关病史？没有明显问题也可以直接选。', STATES
    if scenario['status'] == 'needs_stage':
        return 'food_health_stage', '这个问题现在还在发生、正在恢复，还是已经恢复稳定了？', {k: v for k, v in STAGES.items() if k != 'none'}
    return None
