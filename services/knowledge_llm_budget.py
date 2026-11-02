"""Process-local LLM budget, usage audit, and resilient HTTP calls."""
from __future__ import annotations

import json
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Callable


class BudgetExceeded(RuntimeError):
    pass


_lock = Lock()
_state: dict[str, Any] = {
    "max_calls": 300, "max_tokens": 1_000_000, "calls": 0,
    "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
    "audit_path": None,
}


def begin(*, max_calls: int, max_tokens: int, audit_path: str | Path | None = None) -> None:
    with _lock:
        _state.update(max_calls=max(1, int(max_calls)), max_tokens=max(1, int(max_tokens)),
                      calls=0, prompt_tokens=0, completion_tokens=0, total_tokens=0,
                      audit_path=Path(audit_path) if audit_path else None)


def snapshot() -> dict[str, int]:
    with _lock:
        return {key: int(_state[key]) for key in (
            "max_calls", "max_tokens", "calls", "prompt_tokens",
            "completion_tokens", "total_tokens")}


def _audit_locked(entry: dict[str, Any]) -> None:
    """Append one audit record; caller must hold the state lock."""
    path = _state.get("audit_path")
    if not path:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(
            {"at": datetime.now(timezone.utc).isoformat(), **entry},
            ensure_ascii=False, separators=(",", ":")) + "\n")


def chat_post(post: Callable[..., Any], url: str, *, stage: str,
              attempt: int | None = None, **kwargs: Any) -> Any:
    with _lock:
        if _state["calls"] >= _state["max_calls"]:
            raise BudgetExceeded(f"knowledge LLM call budget reached: {_state['max_calls']}")
        if _state["total_tokens"] >= _state["max_tokens"]:
            raise BudgetExceeded(f"knowledge LLM token budget reached: {_state['max_tokens']}")
        _state["calls"] += 1

    payload = kwargs.get("json")
    if isinstance(payload, dict):
        payload.setdefault("enable_thinking", False)
    model = payload.get("model") if isinstance(payload, dict) else None
    response = post(url, **kwargs)
    status_code = int(getattr(response, "status_code", 0) or 0)
    try:
        usage = response.json().get("usage") or {}
    except (AttributeError, TypeError, ValueError):
        usage = {}
    prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    completion = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    total = int(usage.get("total_tokens") or prompt + completion)
    with _lock:
        _state["prompt_tokens"] += prompt
        _state["completion_tokens"] += completion
        _state["total_tokens"] += total
        entry: dict[str, Any] = {
            "stage": stage, "model": model,
            "prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": total,
        }
        if attempt is not None:
            entry["attempt"] = int(attempt)
        if status_code:
            entry["status_code"] = status_code
        if status_code >= 400:
            entry["ok"] = False
        _audit_locked(entry)
    return response


def retry_after_seconds(response: Any, cap: float = 60.0) -> float | None:
    """Parse the Retry-After header (delta-seconds form) if present."""
    headers = getattr(response, "headers", None) or {}
    try:
        value = headers.get("Retry-After")
    except AttributeError:
        return None
    if value is None:
        return None
    try:
        return max(0.0, min(float(value), cap))
    except (TypeError, ValueError):
        return None  # HTTP-date form: fall back to exponential backoff


def fallback_models(primary: str | None) -> list[str]:
    """Fallback model chain from KNOWLEDGE_LLM_FALLBACK_MODELS (comma separated)."""
    raw = os.getenv("KNOWLEDGE_LLM_FALLBACK_MODELS", "qwen3.7-flash")
    return [m.strip() for m in raw.split(",") if m.strip() and m.strip() != primary]


def chat_post_with_retry(post: Callable[..., Any], url: str, *, stage: str,
                         attempts: int = 3, fallback_models: list[str] | None = None,
                         max_sleep: float = 30.0, **kwargs: Any) -> Any:
    """POST with jittered backoff, Retry-After support, and model fallback.

    Retries transient failures (network errors, 408/409/425/429/5xx). On a fatal
    HTTP status (e.g. 400/401/403) or after exhausting attempts for one model,
    the next fallback model is tried. Every attempt is budget-checked and
    audited via chat_post.
    """
    payload = kwargs.get("json") if isinstance(kwargs.get("json"), dict) else {}
    primary = payload.get("model")
    chain = [primary] + [m for m in (fallback_models or []) if m and m != primary]
    retryable = {408, 409, 425, 429, 500, 502, 503, 504}
    last_exc: Exception | None = None
    for model in chain:
        for attempt in range(max(1, int(attempts))):
            request_kwargs = dict(kwargs)
            if isinstance(payload, dict):
                data = dict(payload)
                if model is not None:
                    data["model"] = model
                request_kwargs["json"] = data
            try:
                response = chat_post(post, url, stage=stage, attempt=attempt, **request_kwargs)
                response.raise_for_status()
                return response
            except BudgetExceeded:
                raise
            except Exception as exc:
                last_exc = exc
                error_response = getattr(exc, "response", None)
                status = int(getattr(error_response, "status_code", 0) or 0)
                if status and status not in retryable:
                    break  # fatal for this model; try the next fallback model
                delay = min(2.0 ** attempt, max_sleep)
                hint = retry_after_seconds(error_response)
                if hint is not None:
                    delay = max(delay, min(hint, max_sleep))
                time.sleep(delay + random.uniform(0, 0.5))
    assert last_exc is not None  # every path here records an exception before falling out
    raise last_exc
