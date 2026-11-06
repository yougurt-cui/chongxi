from unittest.mock import patch
import pytest
from flask import Flask
from api.enterprise_api import enterprise_api
from services import merchant_certification_service as svc


@pytest.fixture
def payload():
    return dict(wechat_id='merchant_test', phone='+86 138-0013-8000', merchant_type='品牌商', jump_type='web', shop_link='https://example.com/shop')


def test_normalize_phone_and_keep_shop_information(payload):
    result = svc.validate(payload)
    assert result['phone'] == '13800138000'
    assert result['shop_link'] == payload['shop_link']


@pytest.mark.parametrize('field,value', [('wechat_id',''), ('phone','123'), ('merchant_type',''), ('jump_type','invalid'), ('shop_link','javascript:alert(1)'), ('shop_link','https://user:secret@example.com'), ('shop_link','//example.com'), ('shop_link','https://example.com/a b'), ('shop_link','http://['), ('shop_link','https://example.com/\nattack')])
def test_reject_invalid_values(payload, field, value):
    payload[field] = value
    with pytest.raises(ValueError):
        svc.validate(payload)


@pytest.mark.parametrize('kind,link', [('mini_program','#小程序://测试店铺/abcd'), ('mini_program','pages/shop/index?id=123'), ('other','weixin://example')])
def test_allow_store_jump_metadata(payload, kind, link):
    payload.update(jump_type=kind, shop_link=link)
    assert svc.validate(payload)['shop_link'] == link


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('MINIPROGRAM_ADMIN_TOKEN','merchant-test-token')
    app = Flask(__name__)
    app.secret_key = 'merchant-test-only'
    app.register_blueprint(enterprise_api)
    app.testing = True
    return app.test_client()


@pytest.mark.parametrize('method,path', [('get','/api/admin/merchant-certifications/users?q=test'), ('get','/api/admin/merchant-certifications'), ('post','/api/admin/merchant-certifications'), ('put','/api/admin/merchant-certifications/1'), ('post','/api/admin/merchant-certifications/1/status')])
def test_all_merchant_routes_require_authentication(client, method, path):
    with patch.object(svc, '_connect') as connection:
        assert getattr(client, method)(path, json={}).status_code == 401
        connection.assert_not_called()


def test_admin_can_save_and_actor_is_recorded(client, payload):
    with client.session_transaction() as session:
        session['workbench_user'] = dict(role='data_admin', username='reviewer')
    with patch.object(svc, 'save_certification', return_value={'ok':True,'item':{'id':1}}) as save:
        response = client.post('/api/admin/merchant-certifications', json=payload)
    assert response.status_code == 200
    save.assert_called_once_with(payload, actor='reviewer')


def test_token_can_list_and_missing_record_is_404(client):
    headers={'X-Admin-Token':'merchant-test-token'}
    with patch.object(svc, 'list_certifications', return_value={'ok':True,'items':[],'total':0}) as listing:
        assert client.get('/api/admin/merchant-certifications?q=shop&page=2', headers=headers).status_code == 200
        listing.assert_called_once_with(query='shop', status='', page='2')
    with patch.object(svc, 'save_certification', side_effect=KeyError('missing')):
        assert client.put('/api/admin/merchant-certifications/999', headers=headers, json={}).status_code == 404


def test_invalid_payload_returns_400_without_database_write(client):
    with patch.object(svc, '_connect') as connection:
        response = client.post('/api/admin/merchant-certifications', headers={'X-Admin-Token':'merchant-test-token'}, json=['bad'])
        assert response.status_code == 400
        connection.assert_not_called()


def test_empty_user_search_does_not_list_users():
    with patch.object(svc, '_connect') as connect:
        assert svc.search_wechat_users(' ') == {'ok': True, 'items': [], 'has_more': False}
        connect.assert_not_called()


def test_user_lookup_requires_admin_and_preserves_duplicate_names(client):
    users=[{'id':'a'*32,'nickname':'same','openid':'open1'}, {'id':'b'*32,'nickname':'same','openid':'open2'}]
    with patch.object(svc, 'search_wechat_users', return_value={'ok':True,'items':users,'has_more':False}) as search:
        r=client.get('/api/admin/merchant-certifications/users?q=same',headers={'X-Admin-Token':'merchant-test-token'})
        assert r.status_code==200 and len(r.json['items'])==2
        search.assert_called_once_with('same')


def test_binding_rejects_changed_or_inactive_user(payload):
    from unittest.mock import MagicMock
    conn=MagicMock();conn.__enter__.return_value=conn
    cur=conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value={'openid':'different-user'}
    payload['selected_user_id']='a'*32
    with patch.object(svc,'ensure_table'),patch.object(svc,'_connect',return_value=conn):
        with pytest.raises(ValueError,match='重新搜索'):
            svc.save_certification(payload)
    conn.commit.assert_not_called()
    assert not any('INSERT' in call.args[0] for call in cur.execute.call_args_list)


def test_user_search_escapes_like_wildcards_and_filters_active_users():
    from unittest.mock import MagicMock
    conn=MagicMock();conn.__enter__.return_value=conn
    cur=conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value=[]
    with patch.object(svc,'_connect',return_value=conn):
        svc.search_wechat_users('name_%')
    sql,args=cur.execute.call_args.args
    assert "status='active'" in sql
    assert args[:3]==('%name!_!%%',)*3
