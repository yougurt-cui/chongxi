from services.knowledge_source_assembly_service import assemble


def source(id=1, **changes):
    row=dict(id=id,batch_id=1,source_provider='qwen',source_model='model',species_code='cat',
             breed_code='ragdoll',life_stage_codes=['kitten','adult'],category_code='feeding_nutrition',
             topic_code='digestive_tolerance',content='原始观点',original_condition='布偶猫',need_codes=[],
             standardization_meta={'status':'success'})
    row.update(changes)
    return row


def test_multi_stage_and_provenance_dedup():
    objects, unmatched, pending=assemble([source(),source(2,batch_id=2,source_provider='deepseek')])
    assert len(objects)==2
    entries=objects['cat','ragdoll','adult']['digestive_tolerance']
    assert len(entries)==1
    assert [s['id'] for s in entries[0]['sources']]==[1,2]
    assert not unmatched and not pending


def test_all_stages_not_expanded_or_mixed_and_conflicts_preserved():
    objects,_,_=assemble([source(life_stage_codes=['all_stages']),source(2,content='不同观点')])
    assert len(objects)==3
    assert len(objects['cat','ragdoll','all_stages']['digestive_tolerance'])==1


def test_unmapped_and_pending_are_not_silently_assigned():
    objects,unmatched,pending=assemble([source(topic_code='cardiac_health',category_code='health_symptoms'),
        source(2,standardization_meta={'status':'pending'})])
    assert objects=={} and unmatched==[1] and pending==[2]


def test_category_and_topic_mapping():
    objects,_,_=assemble([source(category_code='pet_profile',topic_code='breed_traits'),
                          source(2,topic_code='protein_nutrition')])
    assert objects['cat','ragdoll','adult']['pet_profile']
    assert objects['cat','ragdoll','adult']['food_selection']
