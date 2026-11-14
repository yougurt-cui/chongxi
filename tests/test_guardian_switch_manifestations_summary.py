import json
from unittest.mock import MagicMock,patch
from services import guardian_task_knowledge as task

LINKS=[dict(source_id=1,matched_metric='fat',detail_source_ids=[11],relation='',manifestations=['粪便油腻','粪便油光']),dict(source_id=2,matched_metric='carbohydrate',detail_source_ids=[22],relation='',manifestations=['软便'])]
CFG=dict(api_key='test',base_url='http://localhost',model='test')


def test_model_merges_manifestations_with_complete_evidence_and_legacy_shape():
 client=MagicMock();client.chat.completions.create.return_value.choices[0].message.content=json.dumps(dict(groups=[dict(text='可留意便便是否偏软、油腻或有油光。',evidence_ids=[0,1,2])]))
 with patch.object(task,'get_chat_model_config',return_value=CFG),patch.object(task,'OpenAI',return_value=client):
  result=task.summarize_switch_manifestations(LINKS)
 assert result['generation']['mode']=='model_summary'
 assert result['links'][0]['source_ids']==[1,2]
 assert result['links'][0]['detail_source_ids']==[11,22]
 assert result['links'][0]['relation']=='' and len(result['links'][0]['manifestations'])==1
 assert LINKS[0]['manifestations']==['粪便油腻','粪便油光']
 client.chat.completions.create.assert_called_once()


def test_bad_sources_missing_coverage_disease_and_outage_preserve_original():
 client=MagicMock()
 with patch.object(task,'get_chat_model_config',return_value=CFG),patch.object(task,'OpenAI',return_value=client):
  for group in [dict(text='可留意软便',evidence_ids=[999]),dict(text='可留意软便',evidence_ids=[0]),dict(text='一定会出现糖尿病',evidence_ids=[0,1,2]),dict(text='可留意软便',evidence_ids=[0,0,1,2])]:
   client.chat.completions.create.return_value.choices[0].message.content=json.dumps(dict(groups=[group]))
   assert task.summarize_switch_manifestations(LINKS)['links']==LINKS
  client.chat.completions.create.side_effect=TimeoutError()
  assert task.summarize_switch_manifestations(LINKS)['links']==LINKS
 with patch.object(task,'OpenAI') as model:
  assert task.summarize_switch_manifestations([])['generation']['mode']=='empty'
  model.assert_not_called()


def test_summary_order_follows_metric_magnitude_even_if_model_reorders_groups():
 client=MagicMock()
 client.chat.completions.create.return_value.choices[0].message.content=json.dumps(dict(groups=[
  dict(text='可能软便。',evidence_ids=[2]),dict(text='可留意粪便油腻或有油光。',evidence_ids=[0,1])]))
 with patch.object(task,'get_chat_model_config',return_value=CFG),patch.object(task,'OpenAI',return_value=client):
  result=task.summarize_switch_manifestations(LINKS)
 assert result['generation']['mode']=='model_summary'
 assert result['links'][0]['matched_metrics']==['fat']
 assert result['links'][1]['matched_metrics']==['carbohydrate']
