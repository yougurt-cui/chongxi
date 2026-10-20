"""A bounded daily-status flow; breed and sex are context, not invented norms."""
import re
from datetime import timedelta
from statistics import mean

import pymysql
from services import miniprogram_daily_record_service as records
from services.miniprogram_cat_profile_service import get_cat_profile

STATE_KEY = '_daily_status'
PATTERNS = {'food_count': r'(?:进食|吃饭|吃粮|喂食)(?:次数)?\s*[:：]?\s*(\d+(?:\.\d+)?)\s*次',
            'water_count': r'(?:饮水|喝水)(?:次数)?\s*[:：]?\s*(\d+(?:\.\d+)?)\s*次',
            'stool_count': r'(?:排便|拉便便|大便)(?:次数)?\s*[:：]?\s*(\d+(?:\.\d+)?)\s*次'}


def requested(message, state):
    if re.search(r'软便|呕吐|腹泻|便血|粮|配方|配料|原料', message) and not any(re.search(pattern, message) for pattern in PATTERNS.values()):
        return False
    if re.search(r'状态解读|(?:最近|近期|当前|今天|日常).{0,8}(?:状态|进食次数|饮水次数|排便次数)', message):
        return True
    return bool(state.get('slots', {}).get(STATE_KEY) and (
        any(re.search(pattern, message) for pattern in PATTERNS.values())
        or re.search(r'没有记录|暂时没有|不知道|不清楚', message)))


def parse_counts(message):
    # Examples, future plans, questions and usual routines aren't dated observations.
    if re.search(r'如果|假如|例如|比如|计划|打算|明天|后天|每天|平时|可能|大概|应该|是否|会不会|有没有|[？?]', message):
        return None
    days = re.findall(r'\d{4}-\d{2}-\d{2}|今天|昨天', message)
    if len(set(days)) != 1:
        return None
    day = days[0]
    if day in {'今天', '昨天'}:
        day = str(records.today() - timedelta(days=1 if day == '昨天' else 0))
    value = {'day': day, 'complete_day': bool(re.search(r'全天|完整一天|一整天', message)) and day != str(records.today())}
    for key, pattern in PATTERNS.items():
        matches = re.findall(pattern, message)
        if len(matches) > 1:
            return None  # Do not add events or silently choose a correction.
        if matches:
            value[key] = matches[0]
    if len(value) == 2:
        return None
    return records.normalize_record(value)


def build_status(pet, observations):
    valid, stale = [], []
    for observation in observations:
        try:
            normalized = records.normalize_record(observation)
        except ValueError:
            continue
        if normalized['day'] < str(records.today() - timedelta(days=13)):
            stale.append(normalized['day'])
            continue
        valid.append({**observation, **normalized})
    by_day = {item['day']: item for item in reversed(valid)}
    rows = sorted(by_day.values(), key=lambda row: row['day'], reverse=True)
    latest = rows[0] if rows else {}
    fields = [{'key': key, 'label': label, 'unit': '次', 'period': 'natural_day', 'type': 'integer',
               'minimum': 0, 'maximum': 999, 'nullable': True, 'required': False}
              for key, label in records.COUNTS.items()]
    missing = [key for key in records.COUNTS if latest.get(key) is None]
    trends = []
    if latest.get('complete_day'):
        for key, label in records.COUNTS.items():
            previous = [row for row in rows[1:] if row.get('complete_day') and row.get(key) is not None
                        and (row.get('metric_sources') or {}).get(key, row.get('source')) ==
                            (latest.get('metric_sources') or {}).get(key, latest.get('source'))]
            if latest.get(key) is None or len(previous) < 2:
                continue
            baseline = mean(row[key] for row in previous[:7])
            trends.append({'metric': key, 'label': label, 'latest': latest[key],
                           'baseline_mean': round(baseline, 2), 'baseline_days': [row['day'] for row in previous[:7]],
                           'direction': 'up' if latest[key] > baseline else 'down' if latest[key] < baseline else 'same'})
    profile = {key: pet.get(key) for key in ('id', 'name', 'animal_type', 'breed', 'sex', 'neutered', 'age_months', 'weight_kg')}
    identity = '、'.join(str(value) for value in (pet.get('name'), pet.get('breed'), {'male': '公', 'female': '母'}.get(pet.get('sex'), pet.get('sex'))) if value)
    lines = [f'**{identity or "当前宠物"} · 日常状态解读**']
    if not latest and stale:
        lines.append('现有记录已超过最近14天，请补充近期实际数据，再解读当前状态。')
    if latest:
        values = [label + str(latest[key]) + '次' for key, label in records.COUNTS.items() if latest.get(key) is not None]
        amounts = [label + str(latest[key]) + unit for key, label, unit in
                   (('water_ml', '饮水量', 'ml'), ('food_g', '进食量', 'g')) if latest.get(key) is not None]
        lines.append(latest['day'] + ('（完整自然日）' if latest.get('complete_day') else '（截至记录时，非完整自然日）') + '：' +
                     '、'.join(values + amounts or ['暂无次数字段']))
        if latest.get('source') == 'client_snapshot':
            lines.append('❗ 以上来自本机记录快照，请核对是否实际填写；不会自动保存为已确认的云端记录。')
    if trends:
        for trend in trends:
            direction = {'up': '增加', 'down': '减少', 'same': '相同'}[trend['direction']]
            lines.append(f'{trend["label"]}：本次{trend["latest"]}次，对照此前{len(trend["baseline_days"])}个完整记录日均值{trend["baseline_mean"]:g}次，记录次数{direction}。')
        lines.append('❗ 以上只描述记录变化，不能据此认定正常、异常或推断病因。')
    elif latest:
        lines.append('❗ 尚无足够的同来源完整日记录作对照，不把半天的数据与完整一天比较。')
    if missing:
        lines.append('请补充' + '、'.join(records.COUNTS[key] for key in missing) +
                     '。可以直接回复“今天进食3次，饮水5次，排便1次”（请替换为实际数据）；有便便形态、饮水量和进食量也可在记录页补充。')
    else:
        lines.append('可以继续记录后续日期，结合食欲、精神和便便形态观察变化。')
    lines.append('当前没有可核验的品种/性别次数参考表，档案用于个体背景，不生成所谓正常次数区间。')
    return {'version': 1, 'status': 'needs_records' if not latest else 'partial' if missing else 'recorded',
            'pet_profile': profile, 'latest': latest, 'trends': trends,
            'missing_fields': missing, 'input_fields': fields, 'stale_record_days': sorted(set(stale)), 'text': '\n\n'.join(lines)}


def handle_status(user_id, conversation, message, state, client_context=None):
    pet_id = conversation.get('pet_id')
    state.update(primary_intent='general_pet_qa', current_step=None, missing_slots=[], followup_count=0)
    state['slots'][STATE_KEY] = {'pet_id': pet_id}
    state['turn_count'] += 1
    if not pet_id:
        return {'response_type': 'text', 'reply': '请先选择宠物档案，再按日期记录进食、饮水和排便次数。',
                'result': {'intent': 'general_pet_qa', 'daily_status': {'version': 1, 'status': 'pet_needed'}}}
    pet = get_cat_profile(user_id, pet_id)
    if not pet:
        raise LookupError('宠物档案不存在')
    observation = parse_counts(message)
    try:
        saved = records.save_daily_record(user_id, pet_id, observation) if observation else None
        stored = records.list_daily_records(user_id, pet_id)['items']
    except pymysql.MySQLError:
        return {'response_type': 'text', 'reply': '日常记录暂时无法保存或读取，请稍后重试；这次未能确认记录结果。',
                'result': {'intent': 'general_pet_qa', 'daily_status': {'version': 1, 'status': 'data_unavailable'}}}
    # Server observations take precedence per day; don't silently add or overwrite with client snapshots.
    legacy = records.normalize_client_records((client_context or {}).get('recent_daily_records'))
    stored_days = {row['day'] for row in stored}
    status = build_status(pet, stored + [row for row in legacy if row['day'] not in stored_days])
    status['saved'] = bool(saved)
    return {'response_type': 'result_card', 'reply': ('已保存这天的实际次数（重复填写按最新值更新，不累加）。\n\n' if saved else '') + status['text'],
            'result': {'intent': 'general_pet_qa', 'daily_status': status, 'food_candidates': [], 'turn_no': state['turn_count']}}
