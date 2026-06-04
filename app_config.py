"""Centralized runtime configuration for the Flask app."""


import os
from pathlib import Path
from typing import Any, Dict

from vendor.csv_mysql_labeling.src.settings import load_settings


def _env_first(name: str, value: Any) -> Any:
    env_value = os.getenv(name)
    return env_value if env_value not in (None, "") else value


def get_mysql_config(*, database: str | None = None, payload_db: Dict[str, Any] | None = None) -> Dict[str, Any]:
    settings = load_settings()
    cfg = dict(settings.mysql)
    cfg.update(payload_db or {})
    cfg["host"] = _env_first("MYSQL_HOST", cfg.get("host", "127.0.0.1"))
    cfg["port"] = int(_env_first("MYSQL_PORT", cfg.get("port", 3306)))
    cfg["user"] = _env_first("MYSQL_USER", cfg.get("user", "root"))
    cfg["password"] = _env_first("MYSQL_PASSWORD", cfg.get("password", ""))
    cfg["database"] = database or _env_first("MYSQL_DATABASE", cfg.get("database", "csv_labeling"))
    cfg["charset"] = _env_first("MYSQL_CHARSET", cfg.get("charset", "utf8mb4"))
    return cfg


def get_feature_mysql_config(payload_db: Dict[str, Any] | None = None) -> Dict[str, Any]:
    settings = load_settings()
    cfg = dict(settings.mysql)
    cfg.update(dict(getattr(settings, "feature_mysql", {}) or {}))
    cfg.update(payload_db or {})
    database = str(cfg.get("database") or os.getenv("FEATURE_MYSQL_DATABASE") or "protein_feature_platform")
    return get_mysql_config(database=database, payload_db=cfg)


def normalize_openai_base_url(url: str | None, default: str = "https://dashscope.aliyuncs.com/compatible-mode/v1") -> str:
    value = (url or default).strip().rstrip("/")
    if value.endswith("/chat/completions"):
        value = value[: -len("/chat/completions")]
    return value or default


def get_qwen_config(payload: Dict[str, Any] | None = None) -> Dict[str, str]:
    payload = dict(payload or {})
    settings = load_settings()
    ocr = dict(settings.ocr or {})
    openai_cfg = dict(settings.openai or {})

    api_key = (
        payload.get("qwen_api_key")
        or payload.get("api_key")
        or os.getenv("DASHSCOPE_API_KEY")
        or os.getenv("QWEN_API_KEY")
        or ocr.get("qwen_api_key")
    )
    base_url = (
        payload.get("qwen_base_url")
        or payload.get("base_url")
        or os.getenv("QWEN_BASE_URL")
        or ocr.get("qwen_base_url")
        or ocr.get("qwen_endpoint")
        or "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )
    model = (
        payload.get("model")
        or os.getenv("QWEN_MODEL")
        or ocr.get("qwen_text_model")
        or ocr.get("qwen_model")
        or openai_cfg.get("qwen_model")
        or "qwen-plus"
    )
    return {
        "api_key": str(api_key or "").strip(),
        "base_url": normalize_openai_base_url(str(base_url or "")),
        "model": str(model or "qwen-plus").strip(),
    }


def get_pipeline_paths() -> Dict[str, Any]:
    settings = load_settings()
    return dict((settings.pipeline or {}).get("paths") or {})


def get_cat_food_upload_root(default: Path | str) -> Path:
    value = (
        os.getenv("CAT_FOOD_UPLOAD_ROOT")
        or os.getenv("CATFOOD_UPLOAD_ROOT")
        or get_pipeline_paths().get("cat_food_upload_dir")
        or get_pipeline_paths().get("upload_image_dir")
        or default
    )
    return Path(str(value)).expanduser()
