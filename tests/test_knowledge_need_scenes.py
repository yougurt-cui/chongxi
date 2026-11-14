import sys,importlib.util
from pathlib import Path
import pytest
from services import knowledge_need_scene_service as s

@pytest.fixture
def dictionary():
    return [{'need_code':c,'need_name':n,'aliases_pipe':a} for c,n,a in [
      ('food_selection','选粮','吃什么粮|选主粮'),('product_recommendation','产品推荐','推荐猫粮'),
      ('food_transition','换粮过渡','换粮|转粮'),('soft_stool','软便','便便软'),('vomiting','呕吐','反胃'),
      ('food_allergy','疑似食物不良反应','食物过敏|食物不耐受'),
      ('digestive_sensitivity','肠胃敏感','消化敏感|敏感肠胃|胃肠敏感|sensitive stomach')]]

@pytest.mark.parametrize('question,code',[
 ('布偶猫推荐吃什么','food_selection'),('猫咪换粮需要几天过渡期？','food_transition'),
 ('肠胃敏感的猫怎么选粮？','digestive_sensitivity'),('消化敏感如何选粮','digestive_sensitivity'),
 ('经常软便的猫怎么选粮？','soft_stool'),('没有可识别场景','unspecified')])
def test_scene_uses_one_shared_dictionary(question,code,dictionary):
    assert s.scene_for_question(question,dictionary)['need_scene_code']==code


def test_model_spelling_and_chinese_aliases_normalize_to_same_code(dictionary):
    codes,unknown=s.normalize_codes(['SOFT_STOOL','soft-stools','软便','消化敏感','sensitive_stomach'],dictionary)
    assert codes==['digestive_sensitivity','soft_stool'] and not unknown


def test_unknown_enums_are_reported_not_invented(dictionary):
    assert s.normalize_codes(['invented_scene'],dictionary)==([],['invented_scene'])


def test_conjunction_and_alternative_scenarios_stay_distinct(dictionary):
    both=s.scene_for_question('软便又呕吐怎么选粮',dictionary)
    either=s.scene_for_question('软便或呕吐怎么选粮',dictionary)
    assert both['need_scene_codes']==either['need_scene_codes']==['soft_stool','vomiting']
    assert both['need_relation']=='and' and either['need_relation']=='or'
    assert both['need_scene_code']!=either['need_scene_code']


def test_sensitivity_is_not_automatically_soft_stool_or_allergy(dictionary):
    row={'need_codes':['soft_stool','food_allergy'],'source_question':'肠胃敏感怎么选粮',
         'content':'肠胃敏感配方方向','standardization_meta':{}}
    assert s.clean_tags(row,dictionary)==(['digestive_sensitivity'],[])
    row['content']='肠胃敏感且出现软便，存在食物不耐受'
    assert s.clean_tags(row,dictionary)==(['digestive_sensitivity','food_allergy','soft_stool'],[])
