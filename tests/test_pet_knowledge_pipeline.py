from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from services.pet_knowledge_pipeline_service import (
    Base,
    Candidate,
    CandidateSource,
    CleanComment,
    CommentTask,
    EvidenceVote,
    Knowledge,
    KnowledgeQuestion,
    Source,
    SUBTOPICS,
    TOPICS,
    _comments_for_posts,
    _validate_comment_points,
    classify_comment,
    jd,
    merge_candidates,
    prepare_comment_tasks,
    read_comments,
    unlink_post,
    validate_items,
    verify_evidence,
)


def _engine_with_sources():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with engine.begin() as db:
        db.execute(text("CREATE TABLE douyin_raw_content (id INTEGER PRIMARY KEY, title TEXT, content TEXT)"))
        db.execute(text("CREATE TABLE douyin_raw_comments (id INTEGER PRIMARY KEY, post_title TEXT, comment_text TEXT)"))
    return engine


def test_expanded_topics_accept_care_and_behavior():
    assert {"常见症状与就医", "口腔与清洁护理", "行为与训练", "领养与品种"} <= set(TOPICS)
    assert "刷牙与口腔" in SUBTOPICS["口腔与清洁护理"]
    items = validate_items([{
        "content": "给幼犬刷牙时应逐步建立对牙刷和口腔触碰的适应。",
        "topic": "口腔与清洁护理", "subtopic": "刷牙与口腔", "intent": "寻求建议",
        "conditions": {"species": "狗", "life_stage": "幼年期"},
        "knowledge_type": "操作建议", "risk": "低",
    }])
    assert items[0]["topic"] == "口腔与清洁护理"


def test_question_detection_is_conservative():
    assert classify_comment("刚到家的幼犬两个多月也要尽早刷牙吗？")["is_question"] is True
    assert classify_comment("请问这种情况应该怎么办")["is_question"] is True
    assert classify_comment("我家幼犬从两个月开始逐步适应刷牙")["content_role"] == "experience"


def test_model_cannot_turn_question_only_sources_into_knowledge():
    question = CleanComment(
        source_channel="douyin", source_comment_key="1", source_post_key="p",
        raw_comment_id="1", cleaned_text="两个月幼犬需要刷牙吗？", content_hash="x",
        content_role="question", is_noise=0, is_question=1, cleaning_version="v",
    )
    valid, rejected = _validate_comment_points([{
        "claim": "两个月幼犬需要开始刷牙。", "topic": "口腔与清洁护理",
        "subtopic": "刷牙与口腔", "knowledge_type": "操作建议", "risk": "中",
        "source_comment_ids": [1],
    }], {"1": question})
    assert valid == []
    assert rejected[0]["reason"] == "来源全部为提问"


def test_comment_link_rejects_blank_and_duplicate_titles():
    engine = _engine_with_sources()
    with engine.begin() as db:
        db.execute(text("INSERT INTO douyin_raw_content VALUES (1,'唯一标题','a'),(2,'重复标题','b'),(3,'重复标题','c'),(4,'','d')"))
        db.execute(text("INSERT INTO douyin_raw_comments VALUES (11,'唯一标题','这是唯一帖子的一条有效评论'),(12,'重复标题','不应该串给任何一个重复标题帖子'),(13,'','空标题评论不能关联')"))
    with Session(engine) as db:
        assert [row["cid"] for row in read_comments(db, "1")] == [11]
        assert read_comments(db, "2") == []
        assert read_comments(db, "4") == []
        batched = _comments_for_posts(db, {1, 2, 3, 4}, per_post=20)
        assert list(batched) == [1]
        assert batched[1][0]["comment_id"] == 11


def test_prepare_comment_tasks_separates_questions_and_noise(monkeypatch):
    import services.pet_knowledge_pipeline_service as pipeline

    engine = _engine_with_sources()
    with engine.begin() as db:
        db.execute(text("INSERT INTO douyin_raw_content VALUES (1,'唯一养宠帖子','正文')"))
        db.execute(text("""INSERT INTO douyin_raw_comments VALUES
          (11,'唯一养宠帖子','我家幼犬从两个月开始逐步适应刷牙'),
          (12,'唯一养宠帖子','刚到家的幼犬两个多月也要尽早刷牙吗？'),
          (13,'唯一养宠帖子','哈哈哈'),
          (14,'唯一养宠帖子','我家幼犬从两个月开始逐步适应刷牙'),
          (15,'唯一养宠帖子','不锈钢食盆便宜耐用而且容易清洁')"""))
    monkeypatch.setattr(pipeline, "_engine", lambda: engine)
    result = prepare_comment_tasks("1", batch_size=25)
    assert result["raw_comments"] == 5
    assert result["usable_comments"] == 2
    assert result["questions"] == 1
    assert result["tasks"] == 1
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(KnowledgeQuestion)) == 1
        assert db.scalar(select(func.count()).select_from(CommentTask)) == 1
        assert db.scalar(select(func.count()).select_from(CleanComment)) == 5
        assert db.scalar(select(func.count()).select_from(CandidateSource)) == 0


def test_reprocessing_reuses_orphaned_identical_knowledge():
    engine = _engine_with_sources()
    conditions = jd({"species": "猫", "life_stage": "全阶段"})
    with Session(engine) as db:
        original = Knowledge(
            content="给猫换粮时应逐步过渡。", topic="日常喂养", subtopic="换粮过渡",
            intent="寻求建议", conditions_json=conditions, knowledge_type="操作建议",
            risk="低", evidence_status="待验证", status="待审核", source_count=1,
        )
        db.add(original)
        db.flush()
        original_id = original.id
        db.add(Source(knowledge_id=original_id, post_id="1"))
        db.commit()

        unlink_post(db, "1")
        candidate = Candidate(
            post_id="1", content=original.content, topic=original.topic, subtopic=original.subtopic,
            intent=original.intent, conditions_json=conditions, knowledge_type=original.knowledge_type,
            risk=original.risk, evidence_status="待验证",
        )
        db.add(candidate)
        db.flush()
        merge_candidates(db, [candidate])
        db.commit()

        assert candidate.knowledge_id == original_id
        assert db.scalar(select(func.count()).select_from(Knowledge)) == 1
        assert db.get(Knowledge, original_id).status == "待审核"
        assert db.scalar(select(func.count()).select_from(EvidenceVote)) == 0


def test_evidence_votes_are_traceable_and_professional_status_is_preserved(monkeypatch):
    import services.pet_knowledge_pipeline_service as pipeline

    engine = _engine_with_sources()
    with engine.begin() as db:
        db.execute(text("INSERT INTO douyin_raw_content VALUES (1,'帖子一','a'),(2,'帖子二','b')"))
        db.execute(text("INSERT INTO douyin_raw_comments VALUES (11,'帖子一','逐步换粮后我家猫适应得更好'),(21,'帖子二','突然换粮确实容易出现软便')"))
    with Session(engine) as db:
        normal = Knowledge(
            content="给猫换粮时应逐步过渡。", topic="日常喂养", subtopic="换粮过渡",
            intent="寻求建议", conditions_json="{}", knowledge_type="操作建议", risk="低",
            evidence_status="待验证", status="待审核",
        )
        professional = Knowledge(
            content="专业审核结论不能被模型覆盖。", topic="日常喂养", subtopic="换粮过渡",
            intent="风险确认", conditions_json="{}", knowledge_type="事实知识", risk="中",
            evidence_status="专业已验证", status="已审核",
        )
        db.add_all([normal, professional])
        db.flush()
        normal_id, professional_id = normal.id, professional.id
        db.add_all([
            Source(knowledge_id=normal_id, post_id="1"), Source(knowledge_id=normal_id, post_id="2"),
            Source(knowledge_id=professional_id, post_id="1"),
        ])
        db.commit()

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": (
                '{"items":['
                f'{{"id":{normal_id},"votes":['
                '{"post_id":1,"evidence_scope":"帖子","evidence_ids":["post:1"],"stance":"赞同"},'
                '{"post_id":2,"evidence_scope":"评论","evidence_ids":["comment:21"],"stance":"赞同"}]},'
                f'{{"id":{professional_id},"votes":[]}}]}}'
            )}}]}

    monkeypatch.setattr(pipeline, "_engine", lambda: engine)
    monkeypatch.setattr(pipeline, "_qwen", lambda: {
        "base_url": "https://example.invalid/v1", "api_key": "test", "model": "test-model",
    })
    monkeypatch.setattr(pipeline.requests, "post", lambda *args, **kwargs: FakeResponse())

    result = verify_evidence(topic="日常喂养", max_buckets=1)
    assert result["failed_buckets"] == []
    with Session(engine) as db:
        assert db.get(Knowledge, normal_id).evidence_status == "社区经验"
        assert db.get(Knowledge, professional_id).evidence_status == "专业已验证"
        votes = db.scalars(select(EvidenceVote).where(EvidenceVote.knowledge_id == normal_id)).all()
        assert {(v.post_id, v.stance) for v in votes} == {("1", "赞同"), ("2", "赞同")}
        assert {(v.post_id, v.evidence_scope) for v in votes} == {("1", "帖子"), ("2", "评论")}
        assert any("突然换粮" in v.excerpts_json for v in votes)
