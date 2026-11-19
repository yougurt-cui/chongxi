import copy
from services import daily_care_fusion_service as fusion
from services import daily_care_knowledge_service as care
from tests.test_daily_care_activities import v4_document
from services.daily_care_activity_adapter import points_for


def row(id=1,provider='qwen',**extra):
    return dict(id=id,task_id=542,source_question='猫如何驱虫',source_provider=provider,source_model='test',
        species_code='cat',breed_code='unknown_cat',life_stage_code='adult',raw_scope={'species':'cat','breed':'unknown','life_stage':'adult'},
        need_code='parasite_prevention',care_activity_code='cat_deworming',care_subtype='internal',
        care_domain='preventive_care',care_category='寄生虫预防',care_type='routine',
        applicable_conditions={'weight_range':'按标签'},schedule={'frequency':0,'frequency_unit':'conditional'},
        review_status='pending',methods=[{'action':'按标签操作'}],**extra)


def test_exact_variants_preserve_values_provenance_and_model_counts():
    a=row();b=row(2,'doubao');b.update(task_id=546,care_category='预防性驱虫',schedule={'frequency':1,'frequency_unit':'month'},applicable_conditions={'weight_range':'适用范围不同'})
    c=copy.deepcopy(a);c.update(id=3,task_id=547)
    groups=fusion.build_groups([b,a,c]);assert len(groups)==1
    p=groups[0];assert p['source_count']==3 and p['model_count']==2
    assert len(p['schedule'])==2 and len(p['applicable_conditions'])==2
    zero=next(v for v in p['schedule'] if v['value']['frequency']==0)
    assert zero['source_detail_ids']==[1,3] and zero['support_model_count']==1
    assert set(p['field_differences']) >= {'schedule','care_category','applicable_conditions'}
    assert p['content_hash']==fusion.build_groups([c,a,b])[0]['content_hash']
    assert p['_source_ids']==[1,2,3]


def test_scopes_subtypes_codes_questions_and_unknowns_never_mix():
    base=row();rows=[base]
    for i,(field,value) in enumerate([('life_stage_code','kitten'),('care_subtype','external'),('care_activity_code','other'),('source_question','另一个问题'),('raw_scope',{'breed':'unmapped'})],2):
        r=copy.deepcopy(base);r.update(id=i);r[field]=value;rows.append(r)
    assert len(fusion.build_groups(rows))==6
    rows[0]['review_status']='rejected'
    assert len(fusion.build_groups(rows))==5


def test_missing_fields_are_not_factual_differences():
    a=row();b=row(2,'doubao');b['schedule']=None
    p=fusion.build_groups([a,b])[0]
    assert 'schedule' not in p['field_differences']
    assert len(p['schedule'])==2


def test_canonical_source_has_no_legacy_duplicates_and_preserves_six_fields():
    point=points_for(v4_document())[0]
    dictionary={'species':[{'species_code':'dog'}],'breeds':[{'species_code':'dog','breed_code':'all_dog','breed_name':'全部'}],'stages':[{'species_code':'dog','stage_code':'adult'}]}
    p=care.source_row(point,dictionary)
    assert not set(p)&{'care_code','care_name','frequency','steps','precautions','stop_conditions','point_data'}
    for field in ('schedule','tools_materials','methods','avoidances','observations','adjustment_rules'):
        assert p[field]==point['care_activity'].get(field)
    assert p['care_activity_code']==point['care_code']
    assert p['evidence_status']==point['evidence_status']
