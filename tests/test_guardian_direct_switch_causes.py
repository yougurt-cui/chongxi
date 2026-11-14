import pytest
from services import guardian_task_knowledge as task


@pytest.mark.parametrize('issue,cause,key,direction',[
 ('WEIGHT_GAIN','绝育后代谢率与能量需求下降','fat','increase'),
 ('SOFT_STOOL','慢性疼痛（如骨关节炎）','fat','increase'),
 ('URINATION_FREQUENCY','糖尿病','carbohydrate','increase'),
 ('DIARRHEA','食物成分不耐受或蛋白源不适','fat','increase'),
 ('SOFT_STOOL','高不可发酵纤维摄入','fiber','increase'),
 ('VOMITING','毛球积聚','fiber','increase'),
 ('CONSTIPATION','纤维支持不足','fiber','increase'),
 ('OILY_COAT','日粮脂肪负担过高','fat','increase'),
 ('SOFT_STOOL','必需脂肪酸缺乏','fat','decrease'),
 ('SOFT_STOOL','自由采食模式','fat','increase'),
])
def test_indirect_disease_behaviour_and_unsupported_subtypes_are_excluded(issue,cause,key,direction):
 assert not task.direct_switch_cause(dict(issue_code=issue),cause,key,direction)


@pytest.mark.parametrize('cause,key,direction',[
 ('高脂饮食','fat','increase'),
 ('难消化碳水摄入偏高','carbohydrate','increase'),
 ('膳食纤维摄入不足','fiber','decrease'),
])
def test_direct_digestive_dietary_causes_remain_eligible(cause,key,direction):
 assert task.direct_switch_cause(dict(issue_code='SOFT_STOOL'),cause,key,direction)
