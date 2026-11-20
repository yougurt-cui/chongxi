from copy import deepcopy

import pytest

from services import community_experience_service as svc


def sample():
    record = {
        "scope": ["cat", "ragdoll", "adult"],
        "object": "成年布偶猫",
        "material": {"观点": "换粮后观察粪便形态"},
    }
    topic = {
        "scenario": "换粮",
        "behavior": "观察便便",
        "result": "",
        "evidence": "观察粪便形态",
        "terms": [
            "布偶猫换粮",
            "布偶换粮便便",
            "布偶换粮记录",
            "布偶便便形态",
            "布偶换粮后大便",
        ],
    }
    return record, topic


def test_merge_preserves_table_qualified_links_quotes_and_scope():
    record, topic = sample()
    refs = [
        ("knowledge_point", 1),
        ("solution_knowledge_point", 1),
        ("knowledge_point", 2),
    ]
    source = dict.fromkeys(refs, record)
    source[refs[-1]] = dict(record, scope=["dog", "all_dog", "adult"])
    topics, links = svc.assemble(source, {ref: [topic, topic] for ref in refs})
    assert len(topics) == 2 and len(links) == 3
    assert {key[:2] for key in links} == set(refs)
    assert all(quotes == ["观察粪便形态"] for quotes in links.values())


def test_descriptive_products_do_not_merge_or_invent_results():
    record, topic = sample()
    topic.update(behavior="", result="")
    other = dict(topic, evidence="第二款产品")
    topics, _ = svc.assemble(
        {("knowledge_point", 1): record}, {("knowledge_point", 1): [topic, other]}
    )
    assert len(topics) == 2
    assert all(fields[3:5] == ("", "") for fields in topics.values())


@pytest.mark.parametrize(
    "field,bad",
    [
        ("terms", ["a"] * 5),
        ("terms", ["猫换粮"] * 5),
        ("terms", ["猫换粮"] * 4),
        ("evidence", "未提到的疗效"),
    ],
)
def test_invalid_extractions_block_publication(field, bad):
    record, topic = sample()
    topic[field] = bad
    with pytest.raises(ValueError):
        svc.validate(record, [topic])


def test_cache_reused_and_invalidated_on_input_change(tmp_path, monkeypatch):
    record, topic = sample()
    calls = []

    def extract(r):
        calls.append(r)
        return [topic]

    monkeypatch.setattr(svc, "extract", extract)
    source = {("knowledge_point", 1): record}
    first = svc.prepare(source, tmp_path, 1)
    assert svc.prepare(source, tmp_path, 1) == first and len(calls) == 1
    changed = deepcopy(source)
    changed[("knowledge_point", 1)]["material"]["观点"] += "，并记录变化"
    svc.prepare(changed, tmp_path, 1)
    assert len(calls) == 2


def test_species_classification_uses_structured_scope():
    assert svc.species_category('["cat", "ragdoll", "adult"]') == '猫'
    assert svc.species_category('["dog", "all_dog", "adult"]') == '狗'
    assert svc.species_category('[]') == '未分类'


def test_term_type_rejects_invalid_values_before_database_access():
    with pytest.raises(ValueError, match='无效搜索词类型'):
        svc.update_term_type('a' * 64, 1, '猫软便', '未经定义')
