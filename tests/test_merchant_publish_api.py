from decimal import Decimal
from unittest.mock import MagicMock, patch
import io
import pytest
from flask import Flask
from PIL import Image
from werkzeug.datastructures import FileStorage
from api.enterprise_api import enterprise_api
from api import merchant_publish_api as api
from services import merchant_product_service as products


@pytest.fixture
def payload():
    return {'image_ids':['a'*32], 'product_name':'冻干鸡胸肉', 'product_content':'产品介绍', 'product_price':'39.90'}


@pytest.mark.parametrize('field,value', [('product_price','NaN'),('product_price','Infinity'),('product_price','-1'),('product_price','0.001'),('product_price','100000000'),('product_price',True),('image_ids',[]),('image_ids',[1]),('image_ids',['a'*32,'a'*32]),('product_name',' '),('merchant_id',True),('user_id','forged')])
def test_publish_validation(payload,field,value):
    payload[field]=value
    with pytest.raises(ValueError):products.validate_publish(payload)


def test_decimal_price(payload):
    assert products.validate_publish(payload)['product_price']==Decimal('39.90')


@pytest.fixture
def client():
    app=Flask(__name__);app.secret_key='test-only';app.testing=True
    app.register_blueprint(enterprise_api)
    return app.test_client()


@pytest.mark.parametrize('method,path', [('get','/api/merchant/identity'),('post','/api/images/upload'),('post','/api/merchant/products/publish'),('get','/api/merchant/products/mine'),('post','/api/merchant/products/SKUtest/bind')])
def test_mutating_and_identity_endpoints_require_login(client,method,path):
    with patch.object(products,'_connect') as connect:
        response=getattr(client,method)(path,json={})
    assert response.status_code==401
    connect.assert_not_called()


def test_publish_uses_session_and_zip_contract(client,payload):
    with patch.object(api,'require_user_by_token',return_value={'id':'real-user'}),patch.object(products,'publish_product',return_value=({'product_id':'SKUtest','post_id':'p1'},True)) as publish:
        response=client.post('/api/merchant/products/publish',json=payload,headers={'Authorization':'Bearer token','Idempotency-Key':'once'})
    assert response.status_code==201 and response.json['code']==0
    publish.assert_called_once_with('real-user',payload,idempotency_key='once')


def test_aliases_and_error_status(client):
    with patch.object(api,'require_user_by_token',return_value={'id':'u'}),patch.object(products,'identity',return_value={'is_merchant':False}):
        assert client.get('/api/miniprogram/merchant/identity',headers={'Authorization':'Bearer token'}).json['data']['is_merchant'] is False
    with patch.object(products,'product_detail',side_effect=LookupError('missing')):
        assert client.get('/api/merchant/products/missing').status_code==404
    with patch.object(api,'require_user_by_token',return_value={'id':'u'}),patch.object(products,'publish_product',side_effect=products.MerchantForbidden('not merchant')):
        assert client.post('/api/merchant/products/publish',json={},headers={'Authorization':'Bearer token'}).status_code==403


def test_image_bytes_are_validated_before_storage():
    conn=MagicMock();conn.__enter__.return_value=conn
    cur=conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value=[{'id':1}]
    with patch.object(products,'ensure_tables'),patch.object(products,'_connect',return_value=conn),patch.object(products.images,'upload_moment_image') as upload:
        with pytest.raises(ValueError):products.upload_image(FileStorage(stream=io.BytesIO(b'not an image'),filename='x.png'),'u')
        upload.assert_not_called()
        picture=io.BytesIO();Image.new('RGB',(2,2)).save(picture,'PNG');picture.seek(0)
        upload.return_value={'item':{'file_id':'a'*32,'url':'/image'}}
        assert products.upload_image(FileStorage(stream=picture,filename='x.png'),'u')['image_id']=='a'*32
