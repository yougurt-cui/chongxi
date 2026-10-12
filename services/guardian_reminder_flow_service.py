"""Reminder dialogue: extract -> ask only missing details -> persist -> reply."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

from services.miniprogram_reminder_service import SHANGHAI, REPEATS, create_reminder, normalize

STATE_KEY = '_reminder_flow'
REPEAT_LABELS = {'none': '不重复', 'daily': '每天', 'weekly': '每周', 'biweekly': '每两周', 'monthly': '每月', 'yearly': '每年'}
TYPE_PATTERNS = {'food': r'买.*粮|买粮', 'litter': r'猫砂', 'fountain': r'清洁.*饮水|清洗.*饮水',
                 'refill': r'添水|加水', 'checkup': r'体检', 'vaccine': r'疫苗', 'bath': r'洗澡', 'groom': r'梳毛|美容'}


def _hour(value):
    if value.isdigit():
        return int(value)
    digits = {'一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}
    if '十' in value:
        left, right = value.split('十', 1)
        return digits.get(left, 1) * 10 + digits.get(right, 0)
    return digits.get(value, -1)


def extract_request(message, *, now=None):
    now = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    result = {}
    date_mentions = re.findall(r'20\d{2}-\d{2}-\d{2}|大后天|后天|明天|明晚|明早|今天|今晚|(?:下周|本周|每周)[一二三四五六日天]', message)
    if len(date_mentions) > 1:
        return {'ambiguous': True}
    for phrase, repeat in (('每两周', 'biweekly'), ('每天', 'daily'), ('每周', 'weekly'), ('每月', 'monthly'), ('每年', 'yearly'), ('不重复', 'none')):
        if phrase in message:
            result['repeat'] = repeat; break
    date = re.search(r'(?<!\d)(20\d{2}-\d{2}-\d{2})(?!\d)', message)
    if date:
        result['date'] = date.group(1)
    else:
        days = next((delta for word, delta in [('大后天', 3), ('后天', 2), ('明天', 1), ('明晚', 1), ('明早', 1), ('今天', 0), ('今晚', 0)] if word in message), None)
        if days is not None:
            result['date'] = (now + timedelta(days=days)).strftime('%Y-%m-%d')
        weekday = re.search(r'(下周|本周|每周)([一二三四五六日天])', message)
        if weekday:
            target = '一二三四五六日'.index(weekday.group(2).replace('天', '日'))
            jump = target - now.weekday()
            if weekday.group(1) == '下周': jump += 7
            elif weekday.group(1) == '每周' and jump < 0: jump += 7
            result['date'] = (now + timedelta(days=jump)).strftime('%Y-%m-%d')
    time = re.search(r'(?<!\d)(\d{1,2}):([0-5]\d)(?!\d)', message)
    clock = re.search(r'([0-9]{1,2}|[一二三四五六七八九十两]{1,3})(?:点|时)(?:([0-5]?\d)分?|半)?', message)
    if time or clock:
        hour = int(time.group(1)) if time else _hour(clock.group(1))
        minute = int(time.group(2)) if time else int(clock.group(2) or (30 if '半' in clock.group(0) else 0))
        if re.search(r'下午|晚上|夜里|今晚|明晚', message) and 1 <= hour < 12:
            hour += 12
        if re.search(r'凌晨', message) and hour == 12:
            hour = 0
        ambiguous_midnight = hour == 12 and bool(re.search(r'晚上|夜里|今晚|明晚', message))
        if 0 <= hour < 24 and 0 <= minute < 60 and not ambiguous_midnight:
            result['time'] = f'{hour:02d}:{minute:02d}'
    if result.get('repeat') == 'daily' and not result.get('date') and result.get('time'):
        due = datetime.fromisoformat(now.strftime('%Y-%m-%d') + 'T' + result['time']).replace(tzinfo=SHANGHAI)
        result['date'] = (due + (timedelta(days=1) if due <= now else timedelta())).strftime('%Y-%m-%d')
    if result.get('repeat') == 'weekly' and result.get('date') == now.strftime('%Y-%m-%d') and result.get('time'):
        due = datetime.fromisoformat(result['date'] + 'T' + result['time']).replace(tzinfo=SHANGHAI)
        if due <= now: result['date'] = (due + timedelta(days=7)).strftime('%Y-%m-%d')
    title = re.sub(r'(?:帮我|请|给我)?(?:设置|设个|新增|添加|创建)?(?:一个|个)?提醒(?:一下)?(?:我)?', '', message)
    title = re.sub(r'^(?:你能不能|能不能|你能|你可以|能否|是否|可以|麻烦|请)', '', title)
    title = re.sub(r'20\d{2}-\d{2}-\d{2}|(?<!\d)\d{1,2}:[0-5]\d|(?:[0-9]{1,2}|[一二三四五六七八九十两]{1,3})(?:点|时)(?:[0-5]?\d分?|半)?', '', title)
    title = re.sub(r'(?:下周|本周|每周)[一二三四五六日天]|每两周|每天|每周|每月|每年|大后天|后天|明天|明晚|明早|今天|今晚|早上|上午|下午|晚上|凌晨|夜里|不重复', '', title).strip(' ，。！？:：')
    title = title.rstrip('吗呢呀？?')
    if title and title not in {'吗', '可以吗', '能吗', '你能', '你可以', '了', '我', '这件事'}:
        result['title'] = title
        kind = next((kind for kind, pattern in TYPE_PATTERNS.items() if re.search(pattern, title)), None)
        if kind: result['type'] = kind
    return result


def handle_reminder(user_id, conversation, message, state, pet, *, interaction=None, now=None):
    draft = state['slots'].setdefault(STATE_KEY, {'stage': 'collecting', 'repeat': 'none'})
    state['primary_intent'] = 'reminder'
    state['turn_count'] += 1
    if re.search(r'取消|不用|不要|别提醒|先不设置', message):
        state['slots'].pop(STATE_KEY, None); state['current_step'] = None
        return {'response_type': 'text', 'reply': '这次提醒设置已取消。已有提醒可以到“我的提醒”中查看或删除。', 'result': {'intent': 'reminder', 'status': 'cancelled'}}
    if draft.get('stage') == 'saved' and re.search(r'提醒|设置|添加|创建', message):
        draft = {'stage': 'collecting', 'repeat': 'none'}; state['slots'][STATE_KEY] = draft
    extracted = extract_request(message, now=now)
    if extracted.get('ambiguous'):
        state['current_step'] = 'reminder_title'
        return {'response_type': 'text', 'reply': '你提到了多个时间。先设置哪一条？请把提醒内容和一个明确时间一起告诉我。', 'result': {'intent': 'reminder', 'status': 'ambiguous'}}
    pending = state.get('current_step')
    if pending == 'reminder_title' and not extracted.get('title') and message:
        extracted['title'] = message.strip()
    if interaction:
        slot, value = interaction.get('slot'), interaction.get('value')
        if slot == 'reminder_repeat' and value in REPEATS:
            extracted['repeat'] = value
        elif slot == 'reminder_title':
            extracted['title'] = str(value or '').strip()
        else:
            raise ValueError('提醒选项无效，请重新填写')
    draft.update(extracted)
    draft['pet_id'] = conversation.get('pet_id') or ''
    for field, question in (('title', '想提醒你做什么？比如买粮、清洁饮水机。'),
                            ('date', '安排在哪一天？可以说“明天”，也可以填写具体日期，例如 ' + ((now or datetime.now(SHANGHAI)) + timedelta(days=1)).strftime('%Y-%m-%d') + '。'),
                            ('time', '当天几点提醒？比如“晚上九点”或“09:00”。')):
        if not draft.get(field):
            state['current_step'] = 'reminder_' + field
            return {'response_type': 'text', 'reply': question, 'result': {'intent': 'reminder', 'status': 'need_more_info', 'missing': field}}
    try:
        data = normalize(draft, now=datetime(1970, 1, 1, tzinfo=timezone.utc))
        # Stable under HTTP retries and chat-state-save retries, scoped to account.
        key = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        result = create_reminder(user_id, {**draft, 'idempotency_key': 'guardian:' + key}, source='guardian')
    except ValueError as error:
        if len(str(draft.get('title') or '')) > 40:
            draft.pop('title', None); state['current_step'] = 'reminder_title'
            return {'response_type': 'text', 'reply': '提醒内容请缩短到40字以内；已选日期和时间会保留。', 'result': {'intent': 'reminder', 'status': 'invalid_title'}}
        draft.pop('date', None); draft.pop('time', None); state['current_step'] = 'reminder_date'
        return {'response_type': 'text', 'reply': str(error) + '。这件事想安排到什么时候？', 'result': {'intent': 'reminder', 'status': 'invalid_time'}}
    draft.update(stage='saved', reminder_id=result['item']['id'])
    state['current_step'] = None
    repeat = REPEAT_LABELS[draft['repeat']]
    name = (pet or {}).get('name') or '宠物'
    return {'response_type': 'result_card', 'reply': f"已为{name}设置「{draft['title']}」：{draft['date']} {draft['time']}，{repeat}（北京时间）。可以在“我的提醒”中查看。当前是小程序内提醒，不会发送系统推送。",
            'result': {'intent': 'reminder', 'status': 'saved', 'reminder': result['item'], 'notification_channel': 'in_app'}}
