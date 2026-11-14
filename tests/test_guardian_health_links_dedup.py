from services.guardian_task_knowledge import deduplicate_health_links


def test_same_source_merges_relations_and_deduplicates_manifestations_without_mutation():
    links = [dict(source_id=1,relation='条件甲',manifestations=['体重增加','体重增加。']),
             dict(source_id=1,relation='条件乙',manifestations=['体重增加','活动量下降'])]
    result=deduplicate_health_links(links)
    assert result == [dict(source_id=1,relation='条件甲；条件乙',manifestations=['体重增加','活动量下降'])]
    assert len(links[0]['manifestations']) == 2
    assert deduplicate_health_links(result) == result


def test_duplicate_manifestations_across_sources_keep_distinct_conditions_and_directions():
    links=[dict(source_id=1,relation='摄入过量时',manifestations=['体重增加']),
           dict(source_id=2,relation='另一条件',manifestations=['体重增加。','体重下降'])]
    result=deduplicate_health_links(links)
    assert len(result)==2
    assert result[1]['manifestations']==['体重下降']
    assert result[1]['relation']=='另一条件'
    assert deduplicate_health_links([])==[]
