import pytest
from services.health_knowledge_import_service import infer_species


@pytest.mark.parametrize('question,explicit,expected',[
    ('猫咪软便是什么原因',None,'cat'),('狗狗软便',None,'dog'),('幼犬消化问题',None,'dog'),
    ('软便是什么原因','cat','cat'),('软便是什么原因','dog','dog'),
    ('cats with loose stools',None,'cat'),('Dogs with loose stools',None,'dog'),
    ('猫和狗的软便',None,None),('软便是什么原因',None,None),
    ('猫咪软便','dog',None),('猫咪软便','{{species}}','cat')])
def test_species_is_explicit_and_conservative(question,explicit,expected):
    assert infer_species(question,explicit)==expected
