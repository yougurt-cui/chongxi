from services.guardian_response_format import compact_health_details
import json
from unittest.mock import MagicMock, patch
import pytest
from services import guardian_task_knowledge as task


def test_four_symptoms_fit_without_losing_facts_or_source_ids():
    original=[{'manifestations':['腹泻','体重下降','带黏液或带血的粪便','精神食欲下降'],
               'supporting':['检查记录'], 'source_ids':[369,10,3]}]
    result=compact_health_details(original)
    assert result[0]['manifestations']==['腹泻；体重下降；带黏液或带血的粪便；精神食欲下降']
    assert result[0]['source_ids']==[369,10,3]
    assert len(original[0]['manifestations'])==4


def test_invalid_or_uncompressible_details_remain_rejectable():
    for items in [['正常',None,'正常','正常'],['超'*101]*4,['长'*100]*4,['正常','','正常','正常']]:
        original=[{'manifestations':items}]
        assert compact_health_details(original)==original
    assert compact_health_details(None) is None


def test_already_valid_groups_are_unchanged():
    original=[{'supporting':['时间关系','饮食记录','检查记录'],'source_ids':[1]}]
    assert compact_health_details(original)==original


def test_real_summary_validator_accepts_packed_lists_but_rejects_unsafe_tail():
    group={'cause':'消化不充分','mechanism':'未消化部分进入后肠。',
           'manifestations':['软便','排气增多','颜色偏深','粪便量增多'],
           'supporting':['饮食记录'], 'source_ids':[1]}
    client=MagicMock();response=client.chat.completions.create.return_value
    with patch.object(task,'get_chat_model_config',return_value={'api_key':'test','base_url':'http://localhost','model':'test'}),patch.object(task,'OpenAI',return_value=client):
        response.choices[0].message.content=json.dumps({'groups':[group]})
        result=task.summarize_health('便臭',[{'id':1}])
        assert '粪便量增多' in result[0]['manifestations'][0]
        group['manifestations'].append('服用5毫克药物')
        response.choices[0].message.content=json.dumps({'groups':[group]})
        with pytest.raises(ValueError,match='Unsafe health summary'):
            task.summarize_health('便臭',[{'id':1}])
