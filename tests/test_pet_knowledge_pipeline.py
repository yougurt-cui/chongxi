from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from services.pet_knowledge_pipeline_service import (
    Base,
    CommentItem,
    Knowledge,
    Source,
    Task,
    SUBTOPICS,
    TOPICS,
    _comments_for_posts,
    _validate_comment_points,
    classify_comment,
    jd,
    merge_items,
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


def _comment_item(post_id, key, text_value, role="statement", **kwargs):
    import hashlib
    return CommentItem(
        post_id=str(post_id), comment_key=str(key), cleaned_text=text_value,
        content_hash=hashlib.sha256(text_value.encode("utf-8")).hexdigest(),
        content_role=role, is_noise=kwargs.get("is_noise", 0),
        is_question=kwargs.get("is_question", 0), cleaning_version="comment-clean-v2",
    )


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
    question = _comment_item("p", "1", "两个月幼犬需要刷牙吗？", role="question", is_question=1)
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
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(CommentItem)) == 5
        assert db.scalar(select(func.count()).select_from(CommentItem).where(
            CommentItem.is_question == 1)) == 1
        assert db.scalar(select(func.count()).select_from(CommentItem).where(
            CommentItem.is_noise == 1)) == 2
        # 幂等：重复执行不产生新行
        prepare_comment_tasks("1", batch_size=25)
        assert db.scalar(select(func.count()).select_from(CommentItem)) == 5


def test_reprocessing_reuses_orphaned_identical_knowledge():
    engine = _engine_with_sources()
    conditions = {"species": "猫", "life_stage": "全阶段"}
    with Session(engine) as db:
        original = Knowledge(
            content="给猫换粮时应逐步过渡。", topic="日常喂养", subtopic="换粮过渡",
            intent="寻求建议", conditions_json=jd(conditions), knowledge_type="操作建议",
            risk="低", evidence_status="待验证", status="待审核", source_count=1,
        )
        db.add(original)
        db.flush()
        original_id = original.id
        db.add(Source(knowledge_id=original_id, post_id="1"))
        db.commit()

        unlink_post(db, "1")
        item = {"content": original.content, "topic": original.topic, "subtopic": original.subtopic,
                "intent": original.intent, "conditions": conditions,
                "knowledge_type": original.knowledge_type, "risk": original.risk}
        merged_ids = merge_items(db, "1", [item])
        db.commit()

        assert merged_ids == [original_id]
        assert db.scalar(select(func.count()).select_from(Knowledge)) == 1
        assert db.get(Knowledge, original_id).status == "待审核"
        assert db.scalar(select(func.count()).select_from(Source)) == 1


def test_extraction_task_ledger_incremental(monkeypatch):
    import services.pet_knowledge_pipeline_service as pipeline

    engine = _engine_with_sources()
    with engine.begin() as db:
        db.execute(text("INSERT INTO douyin_raw_content VALUES (1,'唯一养宠帖子','换粮要逐步过渡')"))
        db.execute(text("INSERT INTO douyin_raw_comments VALUES (11,'唯一养宠帖子','突然换粮确实容易出现软便')"))
    monkeypatch.setattr(pipeline, "_engine", lambda: engine)

    result = pipeline.run_extraction(extractor="demo")
    assert result["updated"] == 1 and result["failed"] == 0
    # 第二次跑：指纹未变，跳过
    result2 = pipeline.run_extraction(extractor="demo")
    assert result2["skipped"] == 1
    with Session(engine) as db:
        task = db.scalars(select(Task)).all()
        assert len(task) == 1
        assert task[0].status == "completed"
        assert task[0].raw_json and "逐步增加新粮比例" in task[0].raw_json
        # demo 抽取在换粮场景产出 2 条知识（逐步过渡 + 短期消化不适）
        assert db.scalar(select(func.count()).select_from(Knowledge)) == 2


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
        # 帖子粒度证据在 knowledge_base.evidence_json
        post_votes = pipeline.jl(db.get(Knowledge, normal_id).evidence_json, [])
        assert {(v["post_id"], v["stance"]) for v in post_votes} == {("1", "赞同")}
        # 评论粒度证据在 comment_item.votes_json（role=independent）
        item = db.scalar(select(CommentItem).where(
            CommentItem.post_id == "2", CommentItem.comment_key == "21"))
        assert item is not None
        votes = pipeline.jl(item.votes_json, [])
        vote = votes[0]
        assert vote["knowledge_id"] == normal_id
        assert vote["stance"] == "赞同" and vote["role"] == "independent"
        assert "突然换粮" in vote["excerpt"]


def test_extraction_aligns_with_existing_knowledge(monkeypatch):
    """方案A：模型标注 same_as_id 时，措辞不同的同一观点直接合并到已有知识。"""
    import services.pet_knowledge_pipeline_service as pipeline

    engine = _engine_with_sources()
    with engine.begin() as db:
        db.execute(text("INSERT INTO douyin_raw_content VALUES (1,'唯一养宠帖子','换粮要逐步过渡')"))
        db.execute(text("INSERT INTO douyin_raw_comments VALUES (11,'唯一养宠帖子','我家猫换粮拉稀了，要慢慢来')"))
    with Session(engine) as db:
        existing = Knowledge(
            content="给猫更换主粮时，可考虑逐步增加新粮比例并观察耐受情况。", topic="日常喂养",
            subtopic="换粮过渡", intent="寻求建议", conditions_json=jd({"species": "猫", "life_stage": "全阶段"}),
            knowledge_type="操作建议", risk="低", evidence_status="待验证", status="待审核", source_count=0,
        )
        db.add(existing)
        db.commit()
        existing_id = existing.id

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": jd({"items": [{
                "content": "换粮要循序渐进，一点点替换旧粮，观察有没有软便。",
                "topic": "日常喂养", "subtopic": "换粮过渡", "intent": "寻求建议",
                "conditions": {"species": "猫", "life_stage": "全阶段"},
                "knowledge_type": "操作建议", "risk": "低", "same_as_id": existing_id}]})}}]}

    monkeypatch.setattr(pipeline, "_engine", lambda: engine)
    monkeypatch.setattr(pipeline, "_qwen", lambda: {
        "base_url": "https://example.invalid/v1", "api_key": "test", "model": "test-model"})
    monkeypatch.setattr(pipeline.requests, "post", lambda *a, **k: FakeResponse())

    result = pipeline.run_extraction(extractor="llm")
    assert result["updated"] == 1
    with Session(engine) as db:
        # 没有新建行，合并回已有知识
        assert db.scalar(select(func.count()).select_from(Knowledge)) == 1
        kb = db.get(Knowledge, existing_id)
        assert kb.source_count == 1
        assert db.scalar(select(func.count()).select_from(Source)) == 1


def test_dedup_merges_semantic_duplicates(monkeypatch):
    """方案B：embedding 召回 + LLM 裁决后合并，sources/votes 重写到主行。"""
    import services.pet_knowledge_pipeline_service as pipeline

    engine = _engine_with_sources()
    with Session(engine) as db:
        a = Knowledge(content="给猫换粮时应逐步过渡，用一周时间慢慢增加新粮的比例并观察耐受情况。",
                      topic="日常喂养", subtopic="换粮过渡", intent="寻求建议", conditions_json="{}",
                      knowledge_type="操作建议", risk="低", evidence_status="待验证", status="待审核")
        b = Knowledge(content="换粮要循序渐进，一点点替换旧粮。", topic="日常喂养", subtopic="换粮过渡",
                      intent="寻求建议", conditions_json="{}", knowledge_type="操作建议", risk="低",
                      evidence_status="待验证", status="待审核")
        db.add_all([a, b])
        db.flush()
        a_id, b_id = a.id, b.id
        db.add_all([Source(knowledge_id=a_id, post_id="1"), Source(knowledge_id=b_id, post_id="2")])
        db.add(_comment_item("2", "21", "换粮要循序渐进，一点点替换旧粮。"))
        db.flush()
        item_id = db.scalar(select(CommentItem.id).where(CommentItem.comment_key == "21"))
        item = db.get(CommentItem, item_id)
        item.votes_json = jd([{"knowledge_id": b_id, "stance": "赞同", "role": "origin", "excerpt": "换粮要循序渐进"}])
        db.commit()

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def fake_post(url, *args, **kwargs):
        if url.endswith("/embeddings"):
            n = len(kwargs["json"]["input"])
            return FakeResponse({"data": [{"index": i, "embedding": [1.0, 0.5, 0.25]} for i in range(n)]})
        return FakeResponse({"choices": [{"message": {"content": jd({"pairs": [
            {"index": 1, "same": True}]})}}]})

    monkeypatch.setattr(pipeline, "_engine", lambda: engine)
    monkeypatch.setattr(pipeline, "_qwen", lambda: {
        "base_url": "https://example.invalid/v1", "api_key": "test", "model": "test-model"})
    monkeypatch.setattr(pipeline.requests, "post", fake_post)

    plan = pipeline.dedup_knowledge(dry_run=True)
    assert plan["same_pairs"] == 1
    assert plan["merge_plan"] == [[min(a_id, b_id), max(a_id, b_id)]]

    result = pipeline.dedup_knowledge()
    assert result["rows_merged"] == 1
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Knowledge)) == 1
        # 主行 = 更长表述
        canonical_id = a_id if db.get(Knowledge, a_id) else b_id
        kb = db.get(Knowledge, canonical_id)
        assert "一周时间" in kb.content
        # 来源并集
        posts = {pid for pid, in db.execute(
            select(Source.post_id).where(Source.knowledge_id == canonical_id)).all()}
        assert posts == {"1", "2"}
        # 评论 votes 重写到主行
        votes = pipeline.jl(db.get(CommentItem, item_id).votes_json, [])
        assert votes[0]["knowledge_id"] == canonical_id


def test_same_as_id_with_mismatched_attributes_is_rejected():
    """修复1：模型幻觉给的 same_as_id 必须通过分类/风险/条件校验，否则回退表层匹配（新建）。"""
    engine = _engine_with_sources()
    with Session(engine) as db:
        existing = Knowledge(
            content="给猫换粮时应逐步过渡。", topic="日常喂养", subtopic="换粮过渡",
            intent="寻求建议", conditions_json=jd({"species": "猫"}),
            knowledge_type="操作建议", risk="低", evidence_status="待验证", status="待审核",
        )
        db.add(existing)
        db.flush()
        existing_id = existing.id
        # 风险不同、条件不同（狗）：即使模型硬说 same_as_id 也不许合并
        items = [
            {"content": "换粮要循序渐进。", "topic": "日常喂养", "subtopic": "换粮过渡",
             "intent": "寻求建议", "conditions": {"species": "猫"},
             "knowledge_type": "操作建议", "risk": "高", "_same_as": existing_id},
            {"content": "狗狗换粮要慢慢来。", "topic": "日常喂养", "subtopic": "换粮过渡",
             "intent": "寻求建议", "conditions": {"species": "狗"},
             "knowledge_type": "操作建议", "risk": "低", "_same_as": existing_id},
        ]
        merged = merge_items(db, "9", items)
        db.commit()
        assert existing_id not in merged
        kb = db.get(Knowledge, existing_id)
        assert kb.risk == "低"
        assert db.scalar(select(func.count()).select_from(Knowledge)) == 3


def test_dedup_never_merges_across_risk_or_species(monkeypatch):
    """修复2：不同风险/物种的知识不进同一桶、冲突对被硬过滤，LLM 裁决不出现在 payload。"""
    import services.pet_knowledge_pipeline_service as pipeline

    engine = _engine_with_sources()
    with Session(engine) as db:
        db.add_all([
            Knowledge(content="给猫换粮时应逐步过渡。", topic="日常喂养", subtopic="换粮过渡",
                      intent="寻求建议", conditions_json=jd({"species": ["猫"]}),
                      knowledge_type="操作建议", risk="低", evidence_status="待验证", status="待审核"),
            Knowledge(content="换粮要循序渐进，一点点替换旧粮。", topic="日常喂养", subtopic="换粮过渡",
                      intent="寻求建议", conditions_json=jd({"species": ["狗"]}),
                      knowledge_type="操作建议", risk="低", evidence_status="待验证", status="待审核"),
            Knowledge(content="逐步增加新粮比例并观察耐受。", topic="日常喂养", subtopic="换粮过渡",
                      intent="寻求建议", conditions_json=jd({"species": ["猫"]}),
                      knowledge_type="操作建议", risk="高", evidence_status="待验证", status="待审核"),
        ])
        db.commit()

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    seen_payloads = []

    def fake_post(url, *args, **kwargs):
        if url.endswith("/embeddings"):
            n = len(kwargs["json"]["input"])
            return FakeResponse({"data": [{"index": i, "embedding": [1.0, 0.5, 0.25]} for i in range(n)]})
        seen_payloads.append(kwargs["json"]["messages"][1]["content"])
        return FakeResponse({"choices": [{"message": {"content": jd({"pairs": [
            {"index": 1, "same": True}, {"index": 2, "same": True}]})}}]})

    monkeypatch.setattr(pipeline, "_engine", lambda: engine)
    monkeypatch.setattr(pipeline, "_qwen", lambda: {
        "base_url": "https://example.invalid/v1", "api_key": "test", "model": "test-model"})
    monkeypatch.setattr(pipeline.requests, "post", fake_post)

    plan = pipeline.dedup_knowledge(dry_run=True)
    # 物种冲突 + 风险不同：三行互不合并，没有候选对进入 LLM
    assert plan["candidates"] == 0
    assert plan["same_pairs"] == 0
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Knowledge)) == 3


def test_dedup_recomputes_evidence_from_union(monkeypatch):
    """修复3：合并后 evidence_status 按证据并集重算，而不是取簇内最优。"""
    import services.pet_knowledge_pipeline_service as pipeline

    engine = _engine_with_sources()
    with Session(engine) as db:
        a = Knowledge(content="给猫换粮时应逐步过渡，用一周时间慢慢增加新粮比例。",
                      topic="日常喂养", subtopic="换粮过渡", intent="寻求建议", conditions_json="{}",
                      knowledge_type="操作建议", risk="低",
                      evidence_status="社区经验", status="待审核",
                      support_posts=2, oppose_posts=0,
                      evidence_json=jd([{"post_id": "1", "stance": "赞同"},
                                        {"post_id": "2", "stance": "赞同"}]))
        b = Knowledge(content="换粮要循序渐进。", topic="日常喂养", subtopic="换粮过渡",
                      intent="寻求建议", conditions_json="{}", knowledge_type="操作建议", risk="低",
                      evidence_status="待验证", status="待审核",
                      support_posts=0, oppose_posts=1,
                      evidence_json=jd([{"post_id": "3", "stance": "反对"}]))
        db.add_all([a, b])
        db.commit()
        a_id, b_id = a.id, b.id

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def fake_post(url, *args, **kwargs):
        if url.endswith("/embeddings"):
            n = len(kwargs["json"]["input"])
            return FakeResponse({"data": [{"index": i, "embedding": [1.0, 0.5, 0.25]} for i in range(n)]})
        return FakeResponse({"choices": [{"message": {"content": jd({"pairs": [
            {"index": 1, "same": True}]})}}]})

    monkeypatch.setattr(pipeline, "_engine", lambda: engine)
    monkeypatch.setattr(pipeline, "_qwen", lambda: {
        "base_url": "https://example.invalid/v1", "api_key": "test", "model": "test-model"})
    monkeypatch.setattr(pipeline.requests, "post", fake_post)

    pipeline.dedup_knowledge()
    with Session(engine) as db:
        kb = db.get(Knowledge, min(a_id, b_id))
        # 并集 = 2 赞同帖 + 1 反对帖 → 有争议（而非继承"社区经验"）
        assert kb.evidence_status == "有争议"
        assert kb.support_posts == 2 and kb.oppose_posts == 1
        votes = pipeline.jl(kb.evidence_json, [])
        assert {(v["post_id"], v["stance"]) for v in votes} == {("1", "赞同"), ("2", "赞同"), ("3", "反对")}


def test_prepare_resets_done_and_removes_deleted_comments(monkeypatch):
    """修复4：评论被编辑后 done→pending、origin 票清除；来源删除的评论行同步清除。"""
    import services.pet_knowledge_pipeline_service as pipeline

    engine = _engine_with_sources()
    with engine.begin() as db:
        db.execute(text("INSERT INTO douyin_raw_content VALUES (1,'唯一养宠帖子','正文')"))
        db.execute(text("INSERT INTO douyin_raw_comments VALUES (11,'唯一养宠帖子','我家幼犬从两个月开始逐步适应刷牙')"))
    monkeypatch.setattr(pipeline, "_engine", lambda: engine)
    prepare_comment_tasks("1", batch_size=25)

    # 模拟蒸馏完成 + 一条已删除评论的残留行
    with Session(engine) as db:
        item = db.scalar(select(CommentItem).where(CommentItem.comment_key == "11"))
        item.status = "done"
        item.votes_json = jd([
            {"knowledge_id": 7, "stance": "赞同", "role": "origin", "excerpt": "旧"},
            {"knowledge_id": 8, "stance": "赞同", "role": "independent", "excerpt": "旧互证"},
        ])
        db.add(_comment_item("1", "999", "这条评论已从来源删除"))
        db.commit()

    # 评论 11 被编辑 + 评论 999 从来源消失
    with engine.begin() as db:
        db.execute(text("UPDATE douyin_raw_comments SET comment_text='我家幼犬八个月才开始适应刷牙，过渡了一个月' WHERE id=11"))
    prepare_comment_tasks("1", batch_size=25)
    with Session(engine) as db:
        item = db.scalar(select(CommentItem).where(CommentItem.comment_key == "11"))
        assert item.status == "pending"
        votes = pipeline.jl(item.votes_json, [])
        # origin 票已清，independent 互证票保留
        assert all(v["role"] != "origin" for v in votes)
        assert any(v["role"] == "independent" for v in votes)
        # 已删评论的行被清除
        assert db.scalar(select(CommentItem.comment_key).where(
            CommentItem.comment_key == "999")) is None


def test_verify_clears_stale_independent_votes(monkeypatch):
    """修复5：上轮命中、本轮未命中的 independent 票会被清除，不与新 support/oppose 矛盾。"""
    import services.pet_knowledge_pipeline_service as pipeline

    engine = _engine_with_sources()
    with engine.begin() as db:
        db.execute(text("INSERT INTO douyin_raw_content VALUES (1,'帖子一','a'),(2,'帖子二','b')"))
        db.execute(text("INSERT INTO douyin_raw_comments VALUES (11,'帖子一','逐步换粮后我家猫适应得更好'),(21,'帖子二','突然换粮确实容易出现软便')"))
    with Session(engine) as db:
        normal = Knowledge(content="给猫换粮时应逐步过渡。", topic="日常喂养", subtopic="换粮过渡",
                           intent="寻求建议", conditions_json="{}", knowledge_type="操作建议", risk="低",
                           evidence_status="待验证", status="待审核")
        db.add(normal)
        db.flush()
        normal_id = normal.id
        db.add_all([Source(knowledge_id=normal_id, post_id="1"),
                    Source(knowledge_id=normal_id, post_id="2")])
        # 模拟上轮留下的 independent 票（挂在评论 21 上，本轮不再命中）
        db.add(_comment_item("2", "21", "突然换粮确实容易出现软便"))
        db.flush()
        item_id = db.scalar(select(CommentItem.id).where(CommentItem.comment_key == "21"))
        db.get(CommentItem, item_id).votes_json = jd([
            {"knowledge_id": normal_id, "stance": "赞同", "role": "independent", "excerpt": "旧证据"}])
        db.commit()

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": (
                '{"items":[{"id":%d,"votes":[{"post_id":1,"evidence_scope":"帖子",'
                '"evidence_ids":["post:1"],"stance":"赞同"}]}]}' % normal_id)}}]}

    monkeypatch.setattr(pipeline, "_engine", lambda: engine)
    monkeypatch.setattr(pipeline, "_qwen", lambda: {
        "base_url": "https://example.invalid/v1", "api_key": "test", "model": "test-model"})
    monkeypatch.setattr(pipeline.requests, "post", lambda *a, **k: FakeResponse())

    verify_evidence(topic="日常喂养", max_buckets=1)
    with Session(engine) as db:
        item = db.get(CommentItem, item_id)
        votes = pipeline.jl(item.votes_json, [])
        # 旧 independent 票被清掉（本轮该评论未被命中）
        assert all(v.get("role") != "independent" for v in votes)
        kb = db.get(Knowledge, normal_id)
        assert kb.support_posts == 1 and kb.oppose_posts == 0


def test_prepare_keeps_history_when_comment_bridge_becomes_ambiguous(monkeypatch):
    import services.pet_knowledge_pipeline_service as pipeline

    engine = _engine_with_sources()
    with engine.begin() as db:
        db.execute(text("INSERT INTO douyin_raw_content VALUES (1,'原本唯一','正文')"))
        db.execute(text("INSERT INTO douyin_raw_comments VALUES (11,'原本唯一','这是一条有效的历史评论')"))
    monkeypatch.setattr(pipeline, "_engine", lambda: engine)
    prepare_comment_tasks("1")
    with engine.begin() as db:
        db.execute(text("INSERT INTO douyin_raw_content VALUES (2,'原本唯一','另一篇正文')"))
    try:
        prepare_comment_tasks("1")
        assert False, "标题不唯一时应中止同步"
    except ValueError:
        pass
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(CommentItem)) == 1


def test_dedup_requires_real_boolean_and_preserves_professional_and_comment_votes(monkeypatch):
    import services.pet_knowledge_pipeline_service as pipeline

    engine = _engine_with_sources()
    with Session(engine) as db:
        long = Knowledge(content="给猫换粮时应逐步增加新粮比例，并持续观察消化耐受情况。",
                         topic="日常喂养", subtopic="换粮过渡", intent="寻求建议",
                         conditions_json="{}", knowledge_type="操作建议", risk="低",
                         evidence_status="待验证", status="待审核")
        reviewed = Knowledge(content="猫换粮应逐步过渡。", topic="日常喂养", subtopic="换粮过渡",
                             intent="寻求建议", conditions_json="{}", knowledge_type="操作建议", risk="低",
                             evidence_status="专业已验证", status="已审核")
        db.add_all([long, reviewed]); db.flush()
        long_id, reviewed_id = long.id, reviewed.id
        vote_item = _comment_item("8", "81", "我家猫逐步换粮后适应得很好")
        vote_item.votes_json = jd([{"knowledge_id": reviewed_id, "stance": "赞同",
                                    "role": "independent", "excerpt": "逐步换粮后适应得很好"}])
        db.add(vote_item); db.commit()

    class FakeResponse:
        def __init__(self, payload): self.payload = payload
        def raise_for_status(self): return None
        def json(self): return self.payload

    llm_same = "false"
    def fake_post(url, *args, **kwargs):
        if url.endswith("/embeddings"):
            n = len(kwargs["json"]["input"])
            return FakeResponse({"data": [{"index": i, "embedding": [1.0, 0.5]} for i in range(n)]})
        return FakeResponse({"choices": [{"message": {"content": jd({"pairs": [
            {"index": 1, "same": llm_same}]})}}]})

    monkeypatch.setattr(pipeline, "_engine", lambda: engine)
    monkeypatch.setattr(pipeline, "_qwen", lambda: {
        "base_url": "https://example.invalid/v1", "api_key": "test", "model": "test-model"})
    monkeypatch.setattr(pipeline.requests, "post", fake_post)
    assert pipeline.dedup_knowledge(dry_run=True)["same_pairs"] == 0

    llm_same = True
    pipeline.dedup_knowledge()
    with Session(engine) as db:
        kb = db.get(Knowledge, long_id)
        assert kb is not None and db.get(Knowledge, reviewed_id) is None
        assert kb.evidence_status == "专业已验证"
        assert kb.support_posts == 1
