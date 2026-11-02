import json
from types import SimpleNamespace

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from services import knowledge_batch_service as batch
from services import pet_knowledge_pipeline_service as pipeline


class Content:
    def __init__(self, text_value):
        self.text = text_value


class FakeFiles:
    def __init__(self):
        self.uploaded = ""
        self.outputs = {}
        self.deleted = []

    def create(self, file, purpose):
        assert purpose == "batch"
        self.uploaded = file.read_text(encoding="utf-8")
        return SimpleNamespace(id="file-input")

    def content(self, file_id):
        return Content(self.outputs[file_id])

    def delete(self, file_id):
        self.deleted.append(file_id)


class FakeBatches:
    def __init__(self):
        self.remote = SimpleNamespace(id="batch-1", status="in_progress",
                                      output_file_id=None, error_file_id=None)

    def create(self, **kwargs):
        assert kwargs["endpoint"] == "/v1/chat/completions"
        assert kwargs["completion_window"] == "24h"
        return self.remote

    def retrieve(self, batch_id):
        assert batch_id == "batch-1"
        return self.remote


class FakeClient:
    def __init__(self):
        self.files = FakeFiles()
        self.batches = FakeBatches()


def make_engine():
    engine = create_engine("sqlite:///:memory:")
    pipeline.Base.metadata.create_all(engine)
    batch.Base.metadata.create_all(engine)
    with engine.begin() as db:
        db.execute(text("CREATE TABLE douyin_raw_content (id INTEGER PRIMARY KEY,title TEXT,content TEXT)"))
        db.execute(text("CREATE TABLE douyin_raw_comments (id INTEGER PRIMARY KEY,post_title TEXT,comment_text TEXT)"))
        db.execute(text("INSERT INTO douyin_raw_content VALUES (1,'唯一帖子','正文')"))
        for idx in range(1, 7):
            db.execute(text("INSERT INTO douyin_raw_comments VALUES (:id,'唯一帖子',:body)"),
                       {"id": idx, "body": f"这是第{idx}条具有足够信息的养宠经验评论"})
    return engine


def test_submit_builds_official_jsonl_and_prevents_duplicate_queue(monkeypatch):
    engine, client = make_engine(), FakeClient()
    monkeypatch.setattr(pipeline, "_engine", lambda: engine)
    monkeypatch.setattr(batch, "_cfg", lambda model=None: {
        "api_key": "test", "base_url": "https://example.invalid/v1", "model": model})
    pipeline.prepare_comment_tasks("1")

    result = batch.submit(engine=engine, client=client, model="qwen3.7-flash",
                          max_requests=10, batch_size=5, per_post_cap=50)
    assert result["requests"] == 2
    lines = [json.loads(line) for line in client.files.uploaded.splitlines()]
    assert lines[0]["url"] == "/v1/chat/completions"
    assert lines[0]["body"]["model"] == "qwen3.7-flash"
    assert lines[0]["body"]["enable_thinking"] is False
    assert batch.submit(engine=engine, client=client, model="qwen3.7-flash")["status"] == "nothing_to_submit"


def test_sync_applies_results_and_records_usage(monkeypatch):
    engine, client = make_engine(), FakeClient()
    monkeypatch.setattr(pipeline, "_engine", lambda: engine)
    monkeypatch.setattr(batch, "_cfg", lambda model=None: {
        "api_key": "test", "base_url": "https://example.invalid/v1", "model": model})
    pipeline.prepare_comment_tasks("1")
    submitted = batch.submit(engine=engine, client=client, model="qwen3.7-flash",
                             max_requests=10, batch_size=25, per_post_cap=50)
    output = {
        "custom_id": "comment-1", "response": {"status_code": 200, "body": {
            "usage": {"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100},
            "choices": [{"message": {"content": json.dumps({"knowledge_points": []})}}],
        }}, "error": None,
    }
    client.files.outputs["file-output"] = json.dumps(output)
    client.batches.remote = SimpleNamespace(id="batch-1", status="completed",
                                            output_file_id="file-output", error_file_id=None)

    result = batch.sync(engine=engine, client=client)[0]
    assert result["applied"] == 1 and result["total_tokens"] == 100
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(pipeline.CommentItem).where(
            pipeline.CommentItem.status == "done")) == 6
        assert db.scalar(select(batch.BatchRequest.status)) == "completed"
    assert set(client.files.deleted) == {"file-input", "file-output"}
