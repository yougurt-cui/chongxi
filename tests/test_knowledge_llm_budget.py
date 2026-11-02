import pytest

from services import knowledge_llm_budget as budget


class Response:
    class HttpError(Exception):
        def __init__(self, response):
            super().__init__(f"HTTP {response.status_code}")
            self.response = response

    def __init__(self, status_code=200, headers=None):
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return {"usage": {"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100}}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise self.HttpError(self)


def test_usage_is_recorded_and_thinking_is_disabled(tmp_path):
    captured = {}
    def post(_url, **kwargs):
        captured.update(kwargs["json"])
        return Response()

    budget.begin(max_calls=2, max_tokens=200, audit_path=tmp_path / "usage.jsonl")
    budget.chat_post(post, "https://example.invalid", stage="test", json={"model": "flash"})
    assert captured["enable_thinking"] is False
    assert budget.snapshot()["total_tokens"] == 100
    assert '"stage":"test"' in (tmp_path / "usage.jsonl").read_text()


def test_call_budget_stops_before_request():
    calls = []
    budget.begin(max_calls=1, max_tokens=200)
    budget.chat_post(lambda *_a, **_k: calls.append(1) or Response(), "x", stage="test", json={})
    try:
        budget.chat_post(lambda *_a, **_k: calls.append(1) or Response(), "x", stage="test", json={})
    except budget.BudgetExceeded:
        pass
    else:
        raise AssertionError("expected budget cutoff")
    assert calls == [1]


def test_retry_succeeds_after_transient_5xx_and_audits_both(tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr(budget.time, "sleep", lambda s: sleeps.append(s))
    responses = [Response(503), Response(200)]

    def post(_url, **_kwargs):
        return responses.pop(0)

    budget.begin(max_calls=5, max_tokens=10_000, audit_path=tmp_path / "usage.jsonl")
    result = budget.chat_post_with_retry(post, "https://example.invalid", stage="test", json={"model": "m1"})
    assert result.status_code == 200
    assert len(sleeps) == 1 and sleeps[0] >= 1.0  # exponential backoff plus jitter
    lines = (tmp_path / "usage.jsonl").read_text().strip().splitlines()
    assert '"status_code":503' in lines[0] and '"ok":false' in lines[0] and '"attempt":0' in lines[0]
    assert '"status_code":200' in lines[1] and '"attempt":1' in lines[1]


def test_retry_after_header_extends_delay(tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr(budget.time, "sleep", lambda s: sleeps.append(s))
    responses = [Response(429, headers={"Retry-After": "7"}), Response(200)]

    def post(_url, **_kwargs):
        return responses.pop(0)

    budget.begin(max_calls=5, max_tokens=10_000)
    budget.chat_post_with_retry(post, "https://example.invalid", stage="test", json={"model": "m1"})
    assert sleeps and sleeps[0] >= 7.0


def test_fatal_status_falls_back_to_next_model(tmp_path, monkeypatch):
    monkeypatch.setattr(budget.time, "sleep", lambda _s: None)
    seen_models = []
    responses = [Response(400), Response(200)]

    def post(_url, **kwargs):
        seen_models.append(kwargs["json"]["model"])
        return responses.pop(0)

    budget.begin(max_calls=5, max_tokens=10_000)
    result = budget.chat_post_with_retry(
        post, "https://example.invalid", stage="test",
        fallback_models=["backup-model"], json={"model": "primary-model"})
    assert result.status_code == 200
    assert seen_models == ["primary-model", "backup-model"]


def test_budget_still_applies_to_retries(monkeypatch):
    monkeypatch.setattr(budget.time, "sleep", lambda _s: None)
    calls = []

    def post(_url, **_kwargs):
        calls.append(1)
        return Response(500)

    budget.begin(max_calls=2, max_tokens=10_000)
    with pytest.raises(budget.BudgetExceeded):  # 2 attempts, then budget cut-off
        budget.chat_post_with_retry(post, "https://example.invalid", stage="test",
                                    attempts=5, json={"model": "m1"})
    assert len(calls) == 2


def test_retry_after_seconds_parses_delta_and_ignores_http_date():
    assert budget.retry_after_seconds(Response(429, headers={"Retry-After": "5"})) == 5.0
    assert budget.retry_after_seconds(Response(429, headers={"Retry-After": "Wed, 21 Oct 2015"})) is None
    assert budget.retry_after_seconds(Response(429)) is None


def test_fallback_models_excludes_primary_and_reads_env(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_LLM_FALLBACK_MODELS", "qwen-plus, qwen-max")
    assert budget.fallback_models("qwen-plus") == ["qwen-max"]
    monkeypatch.delenv("KNOWLEDGE_LLM_FALLBACK_MODELS")
    assert budget.fallback_models("qwen-plus") == ["qwen3.7-flash"]
