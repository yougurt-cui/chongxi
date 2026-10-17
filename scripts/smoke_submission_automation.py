"""Offline business stubs, real isolated local MySQL; refuses production targets."""
import os, sys, uuid, threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from unittest.mock import patch
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flask import Flask, jsonify
from app_config import get_mysql_config
from services import orchestrator_service as j, orchestrator_worker as w, submission_notifications as n
from services import miniprogram_food_submission_service as s
from services.miniprogram_auth_service import init_miniprogram_auth_tables
cfg=get_mysql_config()
assert cfg['host']=='127.0.0.1' and int(cfg['port'])==13306 and cfg['user']=='chongxi_dev'
j.init_db();s.init_food_submission_tables();init_miniprogram_auth_tables();n.init_db()
ids=[];sub=uuid.uuid4().hex;user=uuid.uuid4().hex;hits=[]
app=Flask(__name__)
@app.post('/offline/ocr')
def ocr():
 hits.append('ocr');return jsonify(ok=True,ingredient_composition='鸡肉、鱼油',brand='测试',product_name='离线验证')
@app.post('/offline/finish')
def finish():
 hits.append('finish');return jsonify(ok=True,result='ready')
def make():
 t=j.create_task('catfood_image_analysis', {'source':'offline_automation_smoke'});ids.append(t['id'])
 j.apply_node_result(t['id'],'upload_check',call_status='success',output={'offline':True})
 return t['id']
def sql(q,p=()):
 with j._connect() as c:
  with c.cursor() as cur:
   cur.execute(q,p); result=list(cur.fetchall()) if cur.description else cur.rowcount
  c.commit()
 return result
original=j.PIPELINE_DEFINITIONS['catfood_image_analysis']
j.PIPELINE_DEFINITIONS['catfood_image_analysis']=(
 j.NodeDefinition('upload_check','upload',priority=10),
 j.NodeDefinition('ocr_formula','ocr',api=j.ApiRoute('POST','/offline/ocr'),depends_on=('upload_check',),priority=20),
 j.NodeDefinition('offline_finish','finish',api=j.ApiRoute('POST','/offline/finish'),depends_on=('ocr_formula',),priority=30))
try:
 with patch.object(j,'output_check',return_value=j.CheckResult(True,j.NODE_SUCCESS,'offline evidence')):
  initial=j.create_task('catfood_image_analysis',{'source':'offline_automation_smoke'});ids.append(initial['id'])
  w._execute_task(app,initial['id']);assert j.get_task(initial['id'])['task_status']=='done'
  assert hits==['ocr','finish'];hits.clear()
  t=make(); barrier=threading.Barrier(2)
  def claim():
   barrier.wait();return j.claim_ready_dispatch_jobs(task_id=t,limit=1,claim_prefix='worker:')['jobs']
  with ThreadPoolExecutor(2) as pool: results=list(pool.map(lambda _:claim(), range(2)))
  claimed=[x for r in results for x in r];assert len(claimed)==1,results
  w.recover_interrupted();assert j.get_task(t)['task_status']=='need_review'
  j.reset_node_for_reextract(t,'ocr_formula')
  try:j.apply_node_result(t,'ocr_formula',call_status='success',output={'x':1},claim_token=claimed[0]['claim_token'])
  except ValueError:pass
  assert j.get_task(t)['nodes'][1]['node_status']=='ready'
  w._execute_task(app,t);assert j.get_task(t)['task_status']=='done';assert hits==['ocr','finish'],hits
  completed=j.get_task(t);assert completed['nodes'][0]['node_status']=='success'
  t2=make();job=j.claim_ready_dispatch_jobs(task_id=t2,limit=1,claim_prefix='worker:')['jobs'][0]
  w._defer(job,{'status_code':429});assert not j.claim_ready_dispatch_jobs(task_id=t2,limit=1)['jobs']
  sql("UPDATE pipeline_task_node SET next_attempt_at=DATE_SUB(UTC_TIMESTAMP(),INTERVAL 1 SECOND) WHERE task_id=%s",(t2,))
  w._execute_task(app,t2);assert j.get_task(t2)['task_status']=='done'
  t3=make();job=j.claim_ready_dispatch_jobs(task_id=t3,limit=1,claim_prefix='worker:')['jobs'][0]
  j.cancel_task(t3);j.apply_node_result(t3,'ocr_formula',call_status='success',output={'x':1},claim_token=job['claim_token'])
  assert j.get_task(t3)['task_status']=='cancelled'
  sql("INSERT INTO miniprogram_user(id,openid,status,created_at,updated_at) VALUES(%s,%s,'active',UTC_TIMESTAMP(),UTC_TIMESTAMP())",(user,'offline_'+user))
  sql("INSERT INTO miniprogram_food_submission(id,user_id,claimed_brand,claimed_product_name,orchestrator_task_id,status,recognition_status,created_at,updated_at) VALUES(%s,%s,'离线','测试猫粮',%s,'active','processing',UTC_TIMESTAMP(),UTC_TIMESTAMP())",(sub,user,t))
  s._sync_recognition(sql('SELECT * FROM miniprogram_food_submission WHERE id=%s',(sub,))[0])
  env={'WECHAT_FOOD_RESULT_TEMPLATE_ID':'offline_template','WECHAT_FOOD_RESULT_TEMPLATE_FIELDS':'{"thing1":"product","phrase2":"status","time3":"completed_at"}'}
  with patch.dict(os.environ,env),patch.object(n,'_access_token',return_value='offline-token'),patch.object(n,'_post',return_value={'errcode':0}) as send:
   try:n.subscribe('other-user',sub,'accept');raise AssertionError('owner bypass')
   except LookupError:pass
   n.subscribe(user,sub,'accept');n.reconcile();n.reconcile();assert send.call_count==1,send.call_count
   assert sql('SELECT status FROM miniprogram_submission_notice WHERE submission_id=%s',(sub,))[0]['status']=='sent'
   sql("UPDATE miniprogram_submission_notice SET status='waiting' WHERE submission_id=%s",(sub,))
   send.side_effect=TimeoutError('offline timeout');n.reconcile();n.reconcile()
   assert send.call_count==2
   assert sql('SELECT status FROM miniprogram_submission_notice WHERE submission_id=%s',(sub,))[0]['status']=='unknown'
 print('PASS: real local MySQL migrations, competing claims, interrupted recovery, stale callback, dependency execution, delayed retry, cancellation, private subscription, one send, unknown-outcome suppression')
finally:
 j.PIPELINE_DEFINITIONS['catfood_image_analysis']=original
 sql('DELETE FROM miniprogram_submission_notice WHERE submission_id=%s',(sub,))
 sql('DELETE FROM miniprogram_food_submission WHERE id=%s',(sub,))
 sql('DELETE FROM miniprogram_user WHERE id=%s',(user,))
 for t in ids:
  for table in ('pipeline_node_output','pipeline_task_node'):sql('DELETE FROM '+table+' WHERE task_id=%s',(t,))
  sql('DELETE FROM pipeline_task WHERE id=%s',(t,))
