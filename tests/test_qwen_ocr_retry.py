import io
import json
from urllib.error import HTTPError

from vendor.csv_mysql_labeling.src import ocr_image


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def _config(max_attempts=3):
    return {
        "api_key": "test-key",
        "endpoint": "https://example.test/chat/completions",
        "model": "qwen-vl-ocr",
        "timeout_seconds": 10,
        "max_image_edge": 1600,
        "max_image_bytes": 2_500_000,
        "max_attempts": max_attempts,
    }


def test_qwen_retries_http_429_then_succeeds(monkeypatch, tmp_path):
    image_path = tmp_path / "food.jpg"
    image_path.write_bytes(b"image")
    calls = []

    monkeypatch.setattr(ocr_image, "_encode_image_for_qwen", lambda *args, **kwargs: ("data:image/jpeg;base64,AA==", "sha"))
    monkeypatch.setattr(ocr_image.time, "sleep", lambda seconds: None)

    def fake_urlopen(request, timeout):
        calls.append(request)
        if len(calls) == 1:
            raise HTTPError(request.full_url, 429, "Too Many Requests", {}, io.BytesIO(b"throttled"))
        return _Response({"model": "qwen-test", "choices": [{"message": {"content": "鸡肉、鱼肉"}}]})

    monkeypatch.setattr(ocr_image, "urlopen", fake_urlopen)

    result = ocr_image._call_qwen_ocr(image_path, _config())

    assert len(calls) == 2
    assert "鸡肉、鱼肉" in result["text"]


def test_qwen_retries_transient_json_error_then_succeeds(monkeypatch, tmp_path):
    image_path = tmp_path / "food.jpg"
    image_path.write_bytes(b"image")
    responses = iter([
        _Response({"error": {"code": "Throttling", "message": "rate limit exceeded"}}),
        _Response({"choices": [{"message": {"content": "牛肉"}}]}),
    ])

    monkeypatch.setattr(ocr_image, "_encode_image_for_qwen", lambda *args, **kwargs: ("data:image/jpeg;base64,AA==", "sha"))
    monkeypatch.setattr(ocr_image.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(ocr_image, "urlopen", lambda request, timeout: next(responses))

    result = ocr_image._call_qwen_ocr(image_path, _config())

    assert "牛肉" in result["text"]
