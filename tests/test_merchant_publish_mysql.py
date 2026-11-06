"""Opt-in integration test. Creates and drops a uniquely named isolated MySQL schema."""
import io
import os
import re
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pymysql
import pytest
from flask import Flask
from PIL import Image
from app_config import get_mysql_config
from api.enterprise_api import enterprise_api
from services import merchant_product_service as svc
from services import merchant_certification_service as cert
from services import miniprogram_auth_service as auth
from services import miniprogram_moment_service as moments
from services import miniprogram_moment_image_service as images

pytestmark = pytest.mark.skipif(os.getenv('CHONGXI_MERCHANT_MYSQL_TEST') != '1', reason='explicit opt-in for isolated MySQL schema')


def test_complete_merchant_flow_and_transaction_rollback():
    original=get_mysql_config()
    source=original['database']
    assert re.fullmatch(r'[A-Za-z0-9_]+',source)
    db='qa_merchant_publish_'+uuid.uuid4().hex[:12]
    root_cfg=dict(original);root_cfg.pop('database')
    root=pymysql.connect(**root_cfg,autocommit=True)
    try:
        with root.cursor() as cur:
            cur.execute(f'CREATE DATABASE `{db}` CHARACTER SET utf8mb4')
            for table in [auth.USER_TABLE,auth.SESSION_TABLE, 'merchant_certification',images.TABLE_NAME,
                          moments.TABLE_NAME,moments.COMMENT_TABLE_NAME,moments.LIKE_TABLE_NAME,moments.VIEW_TABLE_NAME]:
                cur.execute(f'CREATE TABLE `{db}`.`{table}` LIKE `{source}`.`{table}`')
        config=dict(original,database=db)
        def connect(autocommit=False):
            return pymysql.connect(**config,cursorclass=pymysql.cursors.DictCursor,autocommit=autocommit)
        with TemporaryDirectory(prefix='merchant-publish-qa-') as directory:
            patches=[patch.object(svc,'_connect',connect),patch.object(cert,'_connect',connect),
                     patch.object(auth,'_connect_app',connect),patch.object(moments,'_connect_app',connect),patch.object(images,'_connect_app',connect),
                     patch.object(cert,'ensure_table'),patch.object(auth,'init_miniprogram_auth_tables'),
                     patch.object(moments,'init_miniprogram_moment_tables'),patch.object(images,'init_miniprogram_moment_image_tables'),
                     patch.object(images,'UPLOAD_DIR',Path(directory)),patch.object(images,'STORAGE_BACKEND','local'),
                     patch.object(svc,'check_moment_payload_with_wechat',return_value={'ok':True})]
            try:
                for item in patches:item.start()
                svc.ensure_tables.cache_clear();svc.ensure_tables()
                seller,buyer,other=[uuid.uuid4().hex for _ in range(3)]
                tokens={}
                with connect() as conn:
                    with conn.cursor() as cur:
                        for uid in [seller,buyer,other]:
                            cur.execute("INSERT INTO miniprogram_user(id,openid,nickname,status,created_at,updated_at) VALUES (%s,%s,'测试用户','active',UTC_TIMESTAMP(),UTC_TIMESTAMP())",(uid,'openid_'+uid))
                            tokens[uid]=auth._create_session(cur,uid)
                        for uid in [seller,other]:
                            cur.execute("INSERT INTO merchant_certification(wechat_id,phone,merchant_type,jump_type,shop_link) VALUES(%s,'13800138000','品牌商','web','https://example.com/shop')",('openid_'+uid,))
                    conn.commit()
                app=Flask(__name__);app.secret_key='test-only';app.register_blueprint(enterprise_api);app.testing=True
                client=app.test_client()
                headers=lambda uid:{'Authorization':'Bearer '+tokens[uid]}
                assert client.get('/api/merchant/identity').status_code==401
                assert not client.get('/api/merchant/identity',headers=headers(buyer)).json['data']['is_merchant']
                identity=client.get('/api/merchant/identity',headers=headers(seller)).json['data']
                assert identity['is_merchant'] and len(identity['merchants'])==1
                assert 'openid' not in str(identity) and 'phone' not in str(identity)
                picture=io.BytesIO();Image.new('RGB',(3,3)).save(picture,'PNG')
                response=client.post('/api/images/upload',headers=headers(seller),data={'file':(io.BytesIO(picture.getvalue()),'photo.png')},content_type='multipart/form-data')
                assert response.status_code==201,response.json
                image_id=response.json['data']['image_id']
                assert len(image_id)==32 and len(list(Path(directory).rglob('*.png')))==1
                payload={'image_ids':[image_id],'product_name':'测试商品','product_content':'测试商品介绍','product_price':'39.90'}
                assert client.post('/api/merchant/products/publish',json=payload,headers=headers(buyer)).status_code==403
                assert client.post('/api/merchant/products/publish',json=payload,headers=headers(other)).status_code==400
                assert client.post('/api/merchant/products/publish',json={**payload,'user_id':seller},headers=headers(buyer)).status_code==400
                assert client.post('/api/images/upload',headers=headers(seller),data={'file':(io.BytesIO(b'notimage'),'photo.png')}).status_code==400
                # Last insert failure must roll back both SKU and community post.
                with root.cursor() as cur:
                    cur.execute(f"CREATE TRIGGER `{db}`.qa_relation_failure BEFORE INSERT ON `{db}`.`{svc.RELATION_TABLE}` FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='qa rollback'")
                failure=client.post('/api/merchant/products/publish',json=payload,headers=headers(seller))
                assert failure.status_code==500
                with connect() as conn:
                    with conn.cursor() as cur:
                        for table in [svc.PRODUCT_TABLE,moments.TABLE_NAME,svc.RELATION_TABLE]:
                            cur.execute(f'SELECT COUNT(*) AS n FROM {table}');assert cur.fetchone()['n']==0
                with root.cursor() as cur:cur.execute(f'DROP TRIGGER `{db}`.qa_relation_failure')
                request_headers={**headers(seller),'Idempotency-Key':'publish-once'}
                result=client.post('/api/merchant/products/publish',json=payload,headers=request_headers)
                assert result.status_code==201,result.json
                product=result.json['data'];sku=product['product_id'];post=product['post_id']
                assert product['user_id']==seller and product['product_price']=='39.90'
                repeat=client.post('/api/merchant/products/publish',json=payload,headers=request_headers)
                assert repeat.status_code==200 and repeat.json['data']['product_id']==sku
                assert client.get('/api/merchant/products/'+sku).json['data']['product_price']=='39.90'
                assert moments.get_moment(post)['products'][0]['product_id']==sku
                assert moments.list_moments()['items'][0]['post_type']=='product'
                extra,foreign=[uuid.uuid4().hex for _ in range(2)]
                with connect() as conn:
                    with conn.cursor() as cur:
                        for pid,uid in [(extra,seller),(foreign,buyer)]:
                            cur.execute(f"INSERT INTO {moments.TABLE_NAME}(id,user_id,category_code,category_name,title,content,images_json,created_at,updated_at) VALUES(%s,%s,'FUNNY','搞笑日常','测试帖子','内容','[]',UTC_TIMESTAMP(),UTC_TIMESTAMP())",(pid,uid))
                    conn.commit()
                bind='/api/merchant/products/'+sku+'/bind'
                assert client.post(bind,json={'post_id':foreign},headers=headers(seller)).status_code==403
                assert client.post(bind,json={'post_id':extra},headers=headers(buyer)).status_code==403
                for _ in range(2):assert client.post(bind,json={'post_id':extra},headers=headers(seller)).status_code==200
                assert moments.get_moment(extra)['products'][0]['product_id']==sku
                mine=client.get('/api/merchant/products/mine',headers=headers(seller)).json['data']
                assert mine['total']==1 and len(mine['items'])==1
                assert client.get('/api/merchant/products/mine?page=0',headers=headers(seller)).status_code==400
                # A hidden canonical post must hide the public SKU and linked product cards.
                with connect() as conn:
                    with conn.cursor() as cur:cur.execute(f"UPDATE {moments.TABLE_NAME} SET status='hidden' WHERE id=%s",(post,))
                    conn.commit()
                assert client.get('/api/merchant/products/'+sku).status_code==404
                assert not moments.get_moment(extra)['products']
                with connect() as conn:
                    with conn.cursor() as cur:
                        cur.execute(f"UPDATE {moments.TABLE_NAME} SET status='active' WHERE id=%s",(post,))
                        cur.execute("UPDATE merchant_certification SET status='disabled' WHERE id=%s",(identity['merchants'][0]['id'],))
                    conn.commit()
                assert not client.get('/api/merchant/identity',headers=headers(seller)).json['data']['is_merchant']
                assert client.get('/api/merchant/products/'+sku).status_code==404
                assert client.post('/api/merchant/products/publish',json=payload,headers=headers(seller)).status_code==403
                assert not moments.get_moment(extra)['products']
                print('Validated auth, exact merchant identity, real image storage, ownership, atomic rollback, publish, retries, feed binding, permissions and revocation')
            finally:
                for item in reversed(patches):item.stop()
                svc.ensure_tables.cache_clear()
    finally:
        assert db.startswith('qa_merchant_publish_') and re.fullmatch(r'[A-Za-z0-9_]+',db)
        with root.cursor() as cur:cur.execute(f'DROP DATABASE IF EXISTS `{db}`')
        root.close()
