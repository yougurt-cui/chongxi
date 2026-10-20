"""Authenticated API/MySQL regressions, strictly limited to isolated local databases."""
from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path
import json
import sys
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flask import Flask
import pymysql
from app_config import get_mysql_config, get_feature_mysql_config
from api.miniprogram_api import miniprogram_api
from services import miniprogram_auth_service as auth
from services import miniprogram_chat_service as chat
from services import guardian_health_summary_service as summary
from services import miniprogram_daily_record_service as daily
from services.guardian_product_portrait_service import load_product_portraits
from services.taobao_sku_import_service import list_standardized_product_options


def run():
    for cfg in (get_mysql_config(), get_feature_mysql_config()):
        assert cfg['host'] == '127.0.0.1' and cfg['port'] == 13306 and cfg['user'] == 'chongxi_dev'
        assert cfg['database'] in {'csv_labeling', 'protein_feature_platform'}
    app = Flask(__name__); app.register_blueprint(miniprogram_api)
    client = app.test_client()
    auth.init_miniprogram_auth_tables()
    daily.init_daily_record_table()
    users, responses = [], []
    with ExitStack() as stack:
        stack.enter_context(patch.object(chat, 'get_chat_model_config', return_value={'api_key': ''}))
        stack.enter_context(patch.object(summary, 'get_chat_model_config', return_value={'api_key': ''}))
        try:
            with pymysql.connect(**get_mysql_config(), cursorclass=pymysql.cursors.DictCursor) as conn:
                with conn.cursor() as cur:
                    for label in ('a', 'b'):
                        user = auth._upsert_user(cur, 'local-1020-' + uuid.uuid4().hex, nickname='1020验证' + label)
                        users.append((user['id'], auth._create_session(cur, user['id'])))
                conn.commit()

            def call(method, path, payload=None, expected=200, owner=0):
                headers = {'Authorization': 'Bearer ' + users[owner][1]} if owner is not None else {}
                r = client.open('/api/miniprogram' + path, method=method, json=payload, headers=headers)
                body = r.get_json()
                assert r.status_code == expected, (path, r.status_code, body)
                if expected < 400:
                    assert body['ok']
                return body

            def conversation(pet):
                return call('POST', '/chat/conversations', {'pet_id': pet}, 201)['conversation']['id']

            def turn(cid, **payload):
                message = call('POST', '/chat/conversations/' + cid + '/messages', payload)['message']
                responses.append(message)
                return message

            options = list_standardized_product_options(compare_available=True, limit=500)['items']
            portraits = load_product_portraits([row['formula_id'] for row in options])
            foods = [row for row in options if row['formula_id'] in portraits]
            assert len(foods) >= 2
            a, b = foods[:2]
            pet = call('POST', '/cat-profiles', {'name': '1020测试猫', 'breed': '英短', 'sex': 'male', 'age_months': 24,
                       'food_product_id': a['product_id'], 'food_formula_id': a['formula_id']}, 201)['item']
            call('POST', '/cat-profiles/' + pet['id'] + '/diet-history', {'brand': b['brand'], 'product': b['product_name'],
                 'product_id': b['product_id'], 'formula_id': b['formula_id']}, 201)
            c = conversation(pet['id'])
            report = turn(c, message='分析一下现在吃的和历史吃过的粮')
            assert len(report['result']['health_analysis']['subjects']) == 2
            assert all(row['formula_id'] and row['status'] != 'unavailable' for row in report['result']['health_analysis']['subjects'])
            assert report['content'].startswith('**猫咪的整体饮食解读**')
            assert report['interaction']['slot'] == 'food_analysis_recommendation'
            old_option = report['interaction']['options'][1]
            declined = turn(c, interaction={'slot': report['interaction']['slot'], 'value': old_option['value']})
            assert declined['result']['analysis_stage'] == 'declined'
            call('POST', '/chat/conversations/' + c + '/messages', {'interaction': {'slot': report['interaction']['slot'], 'value': old_option['value']}}, 400)
            report = turn(c, message='分析一下现在吃的和历史吃过的粮')
            answer = turn(c, interaction={'slot': report['interaction']['slot'], 'value': report['interaction']['options'][0]['value']})
            assert answer['interaction']['slot'] == 'food_health_state'
            print('PASS case4 multi-object overview, legacy yes/no, stale consent rejection and original recommendation questions')

            c = conversation(pet['id'])
            first = turn(c, message='想聊聊我家宠物最近的状态，需要补充哪些信息？')
            assert set(first['result']['daily_status']['missing_fields']) == set(daily.COUNTS)
            saved = turn(c, message='昨天全天进食3次，饮水5次，排便0次')
            assert saved['result']['daily_status']['saved']
            day = str(daily.today() - timedelta(days=1))
            path = '/cat-profiles/' + pet['id'] + '/daily-records'
            rows = call('GET', path)['items']
            assert len(rows) == 1 and rows[0]['stool_count'] == 0
            for _ in range(2):
                call('PUT', path + '/' + day, {'food_count': 4, 'complete_day': True})
            assert call('GET', path)['items'][0]['food_count'] == 4
            call('GET', path, expected=401, owner=None)
            call('GET', path, expected=404, owner=1)
            call('PUT', path + '/' + day, {'food_count': 9}, expected=404, owner=1)
            call('PUT', path + '/' + day, {'food_count': -1}, expected=400)
            other_pet = call('POST', '/cat-profiles', {'name': '1020另一只猫'}, 201)['item']
            assert not call('GET', '/cat-profiles/' + other_pet['id'] + '/daily-records')['items']
            print('PASS daily guidance, chat persistence, zero/missing distinction, retry overwrite, account/pet isolation and validation')

            c = conversation(other_pet['id'])
            chosen = turn(c, message='没有明显问题，想减重，帮我推荐一款粮')
            rec = chosen['result']['recommendation']
            assert rec['goal'] == 'weight_loss'
            assert '减重' in chosen['content']
            if rec['status'] == 'ready':
                before = {row['formula_id'] for row in rec['items']}
                assert '未核验能量' in chosen['content']
                alternative = turn(c, message='换一组')
                next_rec = alternative['result']['recommendation']
                assert not before.intersection(row['formula_id'] for row in next_rec['items'])
                assert next_rec['status'] in {'ready', 'alternatives_exhausted'}
            else:
                assert not rec['items']
            print('PASS case2 weight-loss goal, compact reply and non-overlapping alternatives; actual status=' + rec['status'])
            out = Path(__file__).resolve().parents[2] / 'local-dev/1020-api-results.json'
            out.write_text(json.dumps(responses, ensure_ascii=False, indent=2, default=str))
            out.chmod(0o600)
        finally:
            with pymysql.connect(**get_mysql_config()) as conn:
                with conn.cursor() as cur:
                    for user_id, _ in users:
                        for table in (daily.TABLE, 'miniprogram_chat_message', 'miniprogram_chat_state',
                                      'miniprogram_chat_conversation', 'miniprogram_pet_diet_history',
                                      'miniprogram_cat_profile', auth.SESSION_TABLE, auth.USER_TABLE):
                            # State rows reference conversations rather than a user directly.
                            if table == 'miniprogram_chat_state':
                                cur.execute('DELETE FROM miniprogram_chat_state WHERE conversation_id IN (SELECT id FROM miniprogram_chat_conversation WHERE user_id=%s)', (user_id,))
                            elif table == 'miniprogram_user':
                                cur.execute('DELETE FROM miniprogram_user WHERE id=%s', (user_id,))
                            else:
                                cur.execute('DELETE FROM ' + table + ' WHERE user_id=%s', (user_id,))
                conn.commit()


if __name__ == '__main__':
    run()
