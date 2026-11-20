from unittest.mock import MagicMock

import pytest

from services import guardian_health_conclusion as health
from services import guardian_photo_questions as photo


def test_long_coat_and_label_questions_bind_to_observation():
    for description in ("毛发较长、蓬松", "long-haired white cat"):
        packet = photo.build([{"animal": "cat", "visible": description}], "p", "cat")
        assert [(o["domain"], o["keyword"]) for o in packet["options"]] == [
            ("care", "梳毛"),
            ("selection", "毛发"),
        ]
        assert packet["subject"] == "cat"
    label = photo.build([{"label": "全价猫粮 鸭肉", "animal": "dog"}], "p", None)
    assert label["species"] == "cat" and "鸭肉" in label["options"][0]["question"]
    assert photo.build([{"label": "鸡肉"}], None, None)["species"] is None
    assert photo.build([{}], "p", None) is None


def test_source_is_scoped_to_user_conversation_and_pet():
    connect = MagicMock()
    cursor = connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
    packet = photo.build([{"animal": "dog", "visible": "长毛"}], "p", "dog")
    source = "a" * 32
    cursor.fetchone.return_value = {"result_json": {"photo_guidance": packet}}
    conversation = {"id": "conversation", "pet_id": "p"}
    assert photo.select(connect, "u", conversation, source + ":1") == (
        packet,
        packet["options"][1],
        source,
    )
    assert cursor.execute.call_args.args[1] == (source, "u", "conversation")
    for candidate, value in [
        (conversation, source + ":2"),
        ({**conversation, "pet_id": "different"}, source + ":0"),
        (conversation, "invalid:0"),
    ]:
        with pytest.raises(ValueError):
            photo.select(connect, "u", candidate, value)
    cursor.fetchone.return_value = None
    with pytest.raises(ValueError):
        photo.select(connect, "u", conversation, source + ":0")


def test_conclusion_uses_original_user_evidence_and_last_bubble(monkeypatch):
    memory = {
        "events": [
            {"id": "history", "role": "user", "text": "换粮后便便臭"},
            {"id": "answer", "role": "assistant", "text": "发生呕吐吗？"},
        ]
    }
    current = {"id": "current", "content": "从A粮换成B粮。现在要紧吗？"}
    packet = {
        "show": True,
        "source_id": "history",
        "quote": "便便臭",
        "conclusion": "仅凭气味不能判断疾病，持续异常请联系兽医。",
    }
    monkeypatch.setattr(health, "_model", lambda *a, **kw: packet)
    reply = {"messages": [{"reply": "第一段"}, {"reply": "根据主人描述整理"}]}
    health.attach(reply, memory, current)
    assert "result" not in reply["messages"][0]
    assert reply["messages"][1]["result"]["health_conclusion"]["source_id"] == "history"
    external = {}
    health.attach(external, memory, current, external=True)
    assert "result" not in external
    for change in (
        {"source_id": "answer"},
        {"quote": "便血"},
        {"show": False},
        {"conclusion": "肯定没问题"},
    ):
        monkeypatch.setattr(health, "_model", lambda *a, **kw: {**packet, **change})
        response = {}
        health.attach(response, memory, current)
        assert "result" not in response


def test_model_shortens_overlong_conclusion_without_truncating_warning(monkeypatch):
    responses = iter(
        [
            {
                "show": True,
                "source_id": "u",
                "quote": "软便",
                "conclusion": "观察。" * 30,
            },
            {"conclusion": "先观察，持续软便或精神变差请联系兽医。"},
        ]
    )
    monkeypatch.setattr(health, "_model", lambda *a, **kw: next(responses))
    answer = {}
    health.attach(answer, {}, {"id": "u", "content": "今天软便"})
    assert answer["result"]["health_conclusion"]["text"].endswith("请联系兽医。")


def test_assistant_question_is_not_a_user_symptom(monkeypatch):
    monkeypatch.setattr(
        health,
        "_model",
        lambda *a, **kw: {
            "show": True,
            "source_id": "a",
            "quote": "软便",
            "conclusion": "先观察。",
        },
    )
    reply = {}
    health.attach(
        reply,
        {"events": [{"id": "a", "role": "assistant", "text": "可能软便"}]},
        {"id": "u", "content": "怎么梳毛？"},
    )
    assert "result" not in reply


def test_unlisted_symptom_can_be_assessed(monkeypatch):
    monkeypatch.setattr(
        health,
        "_model",
        lambda *a, **kw: {
            "show": True,
            "source_id": "u",
            "quote": "头一直歪向左边",
            "conclusion": "持续歪头需要兽医评估。",
        },
    )
    reply = {}
    health.attach(reply, {}, {"id": "u", "content": "它的头一直歪向左边"})
    assert reply["result"]["health_conclusion"]["source_id"] == "u"
