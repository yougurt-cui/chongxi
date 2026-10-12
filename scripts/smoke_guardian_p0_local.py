#!/usr/bin/env python3
"""P0 API/auth/MySQL checks restricted to this machine's isolated development DB."""
from contextlib import ExitStack
from pathlib import Path
import sys
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app_config import get_mysql_config, get_feature_mysql_config


def run():
    config = get_mysql_config()
    for cfg in (config, get_feature_mysql_config()):
        assert (cfg['host'], cfg['port'], cfg['user'], cfg['database']) in (
            ('127.0.0.1', 13306, 'chongxi_dev', 'csv_labeling'),
            ('127.0.0.1', 13306, 'chongxi_dev', 'protein_feature_platform'),
        ), 'This smoke test only permits the isolated local databases'
    import pymysql
    from flask import Flask
    from api.miniprogram_api import miniprogram_api
    from services import miniprogram_auth_service as auth
    from services import miniprogram_cat_profile_service as profiles
    from services import miniprogram_chat_service as chat
    from services import miniprogram_diet_history_service as diets
    from services import miniprogram_food_submission_service as uploads

    app = Flask(__name__); app.register_blueprint(miniprogram_api)
    client = app.test_client()
    auth.init_miniprogram_auth_tables()
    diets.init_diet_history_table(); uploads.init_food_submission_tables()
    users = []
    with ExitStack() as mocks:
        mocks.enter_context(patch.object(chat, 'get_chat_model_config', return_value={'api_key': ''}))
        try:
            with pymysql.connect(**config, cursorclass=pymysql.cursors.DictCursor) as conn:
                with conn.cursor() as cursor:
                    for label in ('a', 'b'):
                        user = auth._upsert_user(cursor, 'guardian-p0-smoke-' + uuid.uuid4().hex, nickname='管家P0测试' + label)
                        users.append((user['id'], auth._create_session(cursor, user['id'])))
                conn.commit()
            headers = {'Authorization': 'Bearer ' + users[0][1]}

            def request(method, path, payload=None, expected=200, request_headers=None):
                response = client.open('/api/miniprogram' + path, method=method, json=payload, headers=request_headers or headers)
                body = response.get_json()
                assert response.status_code == expected, (path, response.status_code, body)
                if expected < 400:
                    assert body['ok']
                return body

            pet = request('POST', '/cat-profiles', {'name': 'P0甲', 'age_months': 24, 'diet': {'brand': '甲牌', 'product': 'A'}}, 201)['item']
            other_pet = request('POST', '/cat-profiles', {'name': 'P0乙'}, 201)['item']
            path = '/cat-profiles/' + pet['id'] + '/diet-history'
            current = request('GET', path)['items']
            assert len(current) == 1 and current[0]['is_current'] and not current[0]['started_on']
            # Repeated reads and non-diet updates must not duplicate feeding records.
            request('PATCH', '/cat-profiles/' + pet['id'], {'weight_kg': 4.5})
            assert len(request('GET', path)['items']) == 1
            request('PATCH', '/cat-profiles/' + pet['id'], {'diet': {'brand': '乙牌', 'product': 'B'}})
            records = request('GET', path)['items']
            assert len(records) == 2 and sum(i['is_current'] for i in records) == 1
            assert next(i for i in records if not i['is_current'])['product'] == 'A'
            assert profiles.get_cat_profile(users[0][0], pet['id'])['food_product'] == 'B'
            print('PASS cloud history, atomic food change, no duplicate snapshots')

            # Known dates explicitly supplied by the owner link only this pet's active conversations.
            added = request('POST', path, {'brand': '历史牌', 'product': 'H', 'started_on': '2026-09-01', 'ended_on': '2026-09-30', 'note': '适应情况未知'}, 201)
            # Retrying a confirmed legacy import must not create duplicate unknown-date history.
            legacy = {'brand': '旧设备牌', 'product': 'L', 'legacy_record_id': 'legacy-device-record'}
            first = request('POST', path, legacy, 201)
            second = request('POST', path, legacy, 201)
            assert first['id'] == second['id']
            assert sum(i['id'] == first['id'] for i in request('GET', path)['items']) == 1
            assert not next(i for i in request('GET', path)['items'] if i['id'] == first['id'])['association_available']
            print('PASS idempotent legacy import without invented feeding dates')
            conversation = request('POST', '/chat/conversations', {'pet_id': pet['id']}, 201)['conversation']
            other_conversation = request('POST', '/chat/conversations', {'pet_id': other_pet['id']}, 201)['conversation']
            for c, text, day in ((conversation, '最近软便怎么照顾', '2026-09-15 04:00:00'),
                                 (conversation, '软便这几天吃什么', '2026-09-16 04:00:00'),
                                 (other_conversation, '这次呕吐', '2026-09-15 04:00:00'),
                                 (conversation, '掉毛', '2026-10-01 04:00:00')):
                saved = chat._save_message(conversation_id=c['id'], user_id=users[0][0], role='user', content=text)
                with pymysql.connect(**config) as conn:
                    with conn.cursor() as cursor:
                        cursor.execute('UPDATE miniprogram_chat_message SET created_at=%s WHERE id=%s', (day, saved['id']))
                    conn.commit()
            record = next(i for i in request('GET', path)['items'] if i['id'] == added['id'])
            assert len(record['health_topics']) == 1
            assert record['health_topics'][0]['code'] == 'digestive'
            assert record['health_topics'][0]['conversation_count'] == 1
            assert not request('GET', '/cat-profiles/' + other_pet['id'] + '/diet-history')['items']
            request('GET', path, expected=404, request_headers={'Authorization': 'Bearer ' + users[1][1]})
            request('POST', path, {'brand': '错牌', 'product': '错粮'}, expected=404, request_headers={'Authorization': 'Bearer ' + users[1][1]})
            request('DELETE', path + '/' + added['id'], expected=404, request_headers={'Authorization': 'Bearer ' + users[1][1]})
            request('DELETE', path + '/' + next(i['id'] for i in records if i['is_current']), expected=400)
            assert client.get('/api/miniprogram' + path).status_code == 401
            print('PASS bounded consultation association, conversation counts, pet/account isolation and auth')

            # A profile-write failure rolls back both the feeding snapshot and profile mutation.
            before = request('GET', path)['items']
            with patch.object(profiles, '_now', return_value='invalid-datetime'):
                request('PATCH', '/cat-profiles/' + pet['id'], {'diet': {'brand': '失败牌', 'product': 'FAIL'}}, expected=500)
            assert request('GET', path)['items'] == before
            assert profiles.get_cat_profile(users[0][0], pet['id'])['food_product'] == 'B'
            print('PASS profile/history transaction rollback')

            # Old source-only OCR success cannot reach the answer model, via the real chat API.
            submission_id = uuid.uuid4().hex
            with pymysql.connect(**config) as conn:
                with conn.cursor() as cursor:
                    cursor.execute('''INSERT INTO miniprogram_food_submission
                        (id,user_id,claimed_brand,claimed_product_name,source_id,recognition_status,created_at,updated_at)
                        VALUES (%s,%s,'测试牌','包装正面',1,'success',UTC_TIMESTAMP(),UTC_TIMESTAMP())''', (submission_id, users[0][0]))
                conn.commit()
            upload = request('GET', '/food-submissions/' + submission_id)['item']
            assert upload['recognition_status'] == 'unrecognized' and not upload['can_analyze']
            with patch.object(chat, '_generate_answer') as generate:
                body = request('POST', '/chat/conversations/' + conversation['id'] + '/messages', {'attachments': [{'type': 'food_submission', 'submission_id': submission_id}]})
                generate.assert_not_called()
            assert body['message']['result']['recognition_status'] == 'unrecognized'
            assert '配料表' in body['message']['content']
            print('PASS source-only OCR serialization and chat analysis guard')

            # Use real ranking math with bounded offline catalogue/profile fixtures.
            import pandas as pd
            from consumer_apps import product_recommendation_engine as engine
            from services import guardian_recommendation_service as recommendation
            fixture = {'formula_id': 999998, 'product_id': 999997, 'catalog_key': 'formula:999998', 'brand': '候选牌', 'product_name': 'C',
                       'product_type': '干粮', 'life_stage': 'adult', 'ingredient_composition': '鸭肉，米', 'price': 40, 'price_max_per_jin': 50}
            pool = pd.DataFrame([{'formula_id': 999998, 'product_name': 'C', 'brand': '候选牌', 'protein_quality_score': 85}])
            targets = {'p': {'profile_code': 'p', 'profile_name': '肠胃参考', 'symptom_type': 'soft_stool', 'weights': {'protein_quality': 1}}}
            with patch('sqlalchemy.create_engine') as create_engine, patch.object(engine, 'load_recommendation_profiles', return_value=targets), patch.object(engine, 'load_product_pool', return_value=pool), patch.object(recommendation, 'list_standardized_product_options', return_value={'items': [fixture]}):
                messages = '/chat/conversations/' + conversation['id'] + '/messages'
                request('POST', messages, {'message': '帮我推荐一款主粮'})
                request('POST', messages, {'interaction': {'slot': 'switch_reason', 'value': 'digestive'}})
                body = request('POST', messages, {'message': '想改善肠胃，每斤50元'})
                result = body['message']['result']
                assert result['recommendation']['status'] == 'ready', result
                assert result['food_candidates'][0]['fit_score'] == 85
                assert result['food_candidates'][0]['formula_id'] == fixture['formula_id']
                assert profiles.get_cat_profile(users[0][0], pet['id'])['food_product'] == 'B'
                request('PATCH', '/cat-profiles/' + pet['id'], {'age_months': None})
                body = request('POST', messages, {'message': '继续推荐主粮'})
                assert body['message']['result']['recommendation']['status'] == 'ready'
                candidate = body['message']['result']['food_candidates'][0]
                assert candidate['formula_id'] == fixture['formula_id']
                assert '未按年龄筛选' in candidate['cautions'][-1]
                assert profiles.get_cat_profile(users[0][0], pet['id'])['age_months'] is None
                assert profiles.get_cat_profile(users[0][0], pet['id'])['food_product'] == 'B'
                print('PASS missing age still returns ranked food through real chat API')
                from services import guardian_food_flow_service as food_flow
                with patch.object(food_flow, 'list_standardized_product_options', return_value={'items': [fixture]}), patch.object(profiles, '_resolve_food_formula', return_value={'standard_brand_name': '候选牌', 'display_name': 'C', 'product_id': 999997, 'formula_id': 999998}):
                    planned = request('POST', messages, {'message': '选第一款'})
                    assert planned['message']['result']['food_flow']['stage'] == 'planned'
                    assert planned['message']['result']['switch_plan']['steps']
                    assert profiles.get_cat_profile(users[0][0], pet['id'])['food_product'] == 'B'
                    request('POST', messages, {'message': '还没开始吃'})
                    assert profiles.get_cat_profile(users[0][0], pet['id'])['food_product'] == 'B'
                    started = request('POST', messages, {'message': '已经开始吃这款'})
                    assert started['message']['result']['food_flow']['stage'] == 'started'
                    assert profiles.get_cat_profile(users[0][0], pet['id'])['food_product'] == 'C'
                    history_count = len(request('GET', path)['items'])
                    try:
                        profiles.update_cat_profile(users[0][0], pet['id'], {'food_brand': '旧计划牌', 'food_product': '旧计划粮'},
                                                    expected_food={'food_brand': '乙牌', 'food_product': 'B', 'food_formula_id': None})
                    except ValueError as error:
                        assert '档案已经变化' in str(error)
                    else:
                        raise AssertionError('Stale plan must fail inside locked profile transaction')
                    assert profiles.get_cat_profile(users[0][0], pet['id'])['food_product'] == 'C'
                    assert len(request('GET', path)['items']) == history_count
                    request('POST', messages, {'message': '已经开始吃这款'})
                    assert len(request('GET', path)['items']) == history_count
                    completed = request('POST', messages, {'message': '已经换好了'})
                    assert completed['message']['result']['food_flow']['stage'] == 'completed'
                    request('POST', messages, {'message': '食欲正常'})
                    print('PASS selection -> comparison/plan -> actual feeding confirmation -> completion/feedback and retry idempotency')
            # Only an explicit profile update makes a recommendation into confirmed current feeding.
            with patch.object(profiles, '_resolve_food_formula', return_value={'standard_brand_name': '候选牌', 'display_name': 'C'}):
                request('PATCH', '/cat-profiles/' + pet['id'], {'diet': {'product_id': 999997, 'formula_id': 999998}})
            assert profiles.get_cat_profile(users[0][0], pet['id'])['food_product'] == 'C'
            assert next(i for i in request('GET', path)['items'] if i['is_current'])['formula_id'] == 999998
            request('PATCH', '/cat-profiles/' + pet['id'], {'diet': {'brand': '手填牌', 'product': 'D'}})
            assert profiles.get_cat_profile(users[0][0], pet['id'])['food_formula_id'] is None
            print('PASS actual engine ranking, budget, explicit confirmation, history archive and stale formula clearing')
            request('DELETE', path + '/' + added['id'])
            assert all(i['id'] != added['id'] for i in request('GET', path)['items'])
        finally:
            with pymysql.connect(**config) as conn:
                with conn.cursor() as cursor:
                    for user_id, _ in users:
                        cursor.execute('DELETE s FROM miniprogram_chat_state s JOIN miniprogram_chat_conversation c ON c.id=s.conversation_id WHERE c.user_id=%s', (user_id,))
                        for table in ('miniprogram_chat_message', 'miniprogram_chat_conversation', diets.TABLE, 'miniprogram_food_submission', 'miniprogram_cat_profile', auth.SESSION_TABLE):
                            cursor.execute(f'DELETE FROM `{table}` WHERE user_id=%s', (user_id,))
                        cursor.execute(f'DELETE FROM `{auth.USER_TABLE}` WHERE id=%s', (user_id,))
                conn.commit()
    print('PASS P0 local API/MySQL smoke; temporary users and records cleaned up')


if __name__ == '__main__':
    run()
