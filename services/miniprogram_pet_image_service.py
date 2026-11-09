"""Private pet-photo storage and AI-assisted profile recognition."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pymysql
from openai import OpenAI
from PIL import Image, ImageOps
from werkzeug.datastructures import FileStorage

from app_config import get_mysql_config, get_qwen_config
from services.pet_breed_catalog import catalog, normalize_identity, resolve_breed, confidence


BASE_DIR = Path(__file__).resolve().parents[1]
UPLOAD_DIR = Path(os.getenv("MINIPROGRAM_UPLOAD_ROOT") or BASE_DIR / "var" / "miniprogram_uploads").expanduser()
STORAGE_BACKEND = (os.getenv("MINIPROGRAM_IMAGE_STORAGE") or "local").strip().lower()
TABLE_NAME = "miniprogram_pet_image_upload"
MAX_IMAGE_SIZE = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 30_000_000
MAX_IMAGE_EDGE = 2048
PROMPT_VERSION = "pet-profile-recognition-v2"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _connect(autocommit: bool = False):
    return pymysql.connect(
        **get_mysql_config(), cursorclass=pymysql.cursors.DictCursor, autocommit=autocommit
    )


def _clean(value: Any, max_length: int | None = None) -> str:
    text = str(value or "").strip()
    return text[:max_length] if max_length else text


def _safe_segment(value: Any, fallback: str = "unknown") -> str:
    result = re.sub(r"[^0-9a-zA-Z_-]+", "_", _clean(value)).strip("._")
    return result[:64] or fallback


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _json_loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except json.JSONDecodeError:
        return default


def init_pet_image_tables() -> None:
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                    id CHAR(32) NOT NULL,
                    user_id VARCHAR(128) NOT NULL,
                    profile_id CHAR(32) NULL,
                    image_usage VARCHAR(32) NOT NULL DEFAULT 'recognition',
                    original_filename VARCHAR(512) NOT NULL,
                    storage_path VARCHAR(1024) NOT NULL,
                    content_type VARCHAR(128) NOT NULL,
                    file_size BIGINT NOT NULL,
                    sha256 CHAR(64) NOT NULL,
                    width INT NULL,
                    height INT NULL,
                    recognition_status VARCHAR(16) NOT NULL DEFAULT 'pending',
                    recognition_result_json LONGTEXT NULL,
                    recognition_model VARCHAR(128) NULL,
                    prompt_version VARCHAR(64) NULL,
                    recognition_error TEXT NULL,
                    status VARCHAR(16) NOT NULL DEFAULT 'active',
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    PRIMARY KEY (id),
                    KEY idx_pet_image_user (user_id,status,created_at),
                    KEY idx_pet_image_profile (profile_id,status),
                    KEY idx_pet_image_sha256 (sha256)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
        conn.commit()


def _prepare_image(image_file: FileStorage) -> tuple[bytes, int, int]:
    if not image_file or not image_file.filename:
        raise ValueError("请上传宠物照片")
    image_file.stream.seek(0)
    raw = image_file.read(MAX_IMAGE_SIZE + 1)
    image_file.stream.seek(0)
    if not raw:
        raise ValueError("图片文件为空")
    if len(raw) > MAX_IMAGE_SIZE:
        raise ValueError("图片大小不能超过 10MB")
    try:
        with Image.open(io.BytesIO(raw)) as source:
            if source.width * source.height > MAX_IMAGE_PIXELS:
                raise ValueError("图片像素尺寸过大")
            image = ImageOps.exif_transpose(source).convert("RGB")
            image.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=88, optimize=True)
            return output.getvalue(), image.width, image.height
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("无法读取图片，请上传有效的 jpg、png 或 webp 文件") from exc


def _oss_config() -> dict[str, str]:
    return {
        "access_key_id": _clean(os.getenv("ALIYUN_OSS_ACCESS_KEY_ID") or os.getenv("OSS_ACCESS_KEY_ID")),
        "access_key_secret": _clean(os.getenv("ALIYUN_OSS_ACCESS_KEY_SECRET") or os.getenv("OSS_ACCESS_KEY_SECRET")),
        "endpoint": _clean(os.getenv("ALIYUN_OSS_ENDPOINT") or os.getenv("OSS_ENDPOINT")),
        "bucket": _clean(os.getenv("ALIYUN_OSS_BUCKET") or os.getenv("OSS_BUCKET")),
        "prefix": _clean(os.getenv("ALIYUN_OSS_PET_PREFIX") or "miniprogram/pets"),
        "signed_url_expires": _clean(os.getenv("ALIYUN_OSS_SIGNED_URL_EXPIRES") or "3600"),
    }


def _oss_bucket(config: dict[str, str], bucket_name: str | None = None):
    import oss2

    missing = [key for key in ("access_key_id", "access_key_secret", "endpoint", "bucket") if not config[key]]
    if missing:
        raise RuntimeError(f"OSS 配置缺失：{', '.join(missing)}")
    return oss2.Bucket(
        oss2.Auth(config["access_key_id"], config["access_key_secret"]),
        config["endpoint"],
        bucket_name or config["bucket"],
    )


def _store_image(content: bytes, *, user_id: str, image_id: str) -> str:
    now = datetime.now(timezone.utc)
    relative = f"{now:%Y/%m/%d}/{_safe_segment(user_id)}/{image_id}.jpg"
    if STORAGE_BACKEND == "oss":
        config = _oss_config()
        object_key = f"{config['prefix'].strip('/')}/{relative}"
        _oss_bucket(config).put_object(object_key, content, headers={"Content-Type": "image/jpeg"})
        return f"oss://{config['bucket']}/{object_key}"
    if STORAGE_BACKEND not in {"", "local"}:
        raise ValueError(f"不支持的图片存储后端：{STORAGE_BACKEND}")
    path = UPLOAD_DIR / "pets" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return str(path)


def _parse_json_object(raw: str) -> dict[str, Any]:
    text = _clean(raw)
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.I | re.S)
    candidate = fenced.group(1) if fenced else text
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("视觉模型未返回有效 JSON")
        data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("视觉模型结果必须是 JSON 对象")
    return data


def _confidence(value: Any) -> float:
    try:
        return round(max(0.0, min(1.0, float(value))), 4)
    except (TypeError, ValueError):
        return 0.0


def _normalize_recognition(data: dict[str, Any]) -> dict[str, Any]:
    age = data.get("age") if isinstance(data.get("age"), dict) else {}
    weight = data.get("weight") if isinstance(data.get("weight"), dict) else {}
    health = data.get("visible_health") if isinstance(data.get("visible_health"), dict) else {}
    result = {
        "age": {
            "stage": _clean(age.get("stage"), 32),
            "label": _clean(age.get("label"), 64),
            "estimated_years": age.get("estimated_years"),
            "minimum_years": age.get("minimum_years"),
            "maximum_years": age.get("maximum_years"),
            "confidence": _confidence(age.get("confidence")),
            "requires_confirmation": True,
        },
        "weight": {
            "estimated_kg": weight.get("estimated_kg"),
            "minimum_kg": weight.get("minimum_kg"),
            "maximum_kg": weight.get("maximum_kg"),
            "confidence": _confidence(weight.get("confidence")),
            "requires_manual_input": True,
        },
        "visible_health": {
            "status": _clean(health.get("status"), 64),
            "label": _clean(health.get("label"), 128),
            "confidence": _confidence(health.get("confidence")),
            "observations": [_clean(item, 128) for item in (health.get("observations") or []) if _clean(item)][:10],
        },
    }
    result.update(normalize_identity(data))
    species = result['animal_type']['value']
    raw_breed = data.get('breed') if isinstance(data.get('breed'), dict) else {}
    candidates = raw_breed.get('candidates')
    candidates = candidates if isinstance(candidates, list) else []
    ranked = []
    for item in [raw_breed, *candidates]:
        if not isinstance(item, dict):
            continue
        match = resolve_breed(species, item.get('value') or item.get('standard_id'))
        if match and not match['standard_id'].endswith('unknown'):
            ranked.append({**match, 'confidence': confidence(item.get('confidence'))})
    if ranked:
        result['breed'] = {**max(ranked, key=lambda item: item['confidence']),
                           'requires_confirmation': True, 'status': 'suggested'}
    result['breed'].pop('candidates', None)
    sex = secrets.choice(('male', 'female'))
    result['sex'] = {'value': sex, 'label': '公' if sex == 'male' else '母',
                     'source': 'random', 'requires_confirmation': True}
    return result


def recognize_pet(content: bytes) -> tuple[dict[str, Any], str]:
    cfg = get_qwen_config({"model": os.getenv("QWEN_VISION_MODEL") or "qwen3-vl-plus"})
    if not cfg["api_key"]:
        raise RuntimeError("未配置通义千问 API Key")
    schema = {
        "animal_type": {"value": "cat|dog|unknown", "label": "猫咪", "confidence": 0.0},
        "breed": {"value": "品种或空字符串", "confidence": 0.0, "candidates": [{"value": "候选品种", "confidence": 0.0}]},
        "appearance": {key: {"value": "", "confidence": 0.0} for key in ("color", "pattern", "length")},
        "age": {"stage": "juvenile|adult|senior|unknown", "label": "成年", "estimated_years": None, "minimum_years": None, "maximum_years": None, "confidence": 0.0},
        "weight": {"estimated_kg": None, "minimum_kg": None, "maximum_kg": None, "confidence": 0.0},
        "visible_health": {"status": "unknown", "label": "仅描述可见外观", "confidence": 0.0, "observations": []},
    }
    prompt = (
        "分析照片中的主要宠物，为建档表单提供保守建议。只描述图像中可见内容，不诊断疾病；"
        "品种只能从下方对应猫或狗的中文品种库中选择，必须使用中文标准名称。"
        "三花、橘猫、奶牛花色、玳瑁属于花色，不是品种；花色和毛长分别填写 appearance。"
        "例如三花短毛家猫可返回品种普通短毛家猫、color黑白橘、pattern三花、length短毛；"
        "花色不能作为确认中华田园猫、波斯猫等品种或血统的依据。"
        "无法判断具体品种时使用猫品种待确认或犬种待确认，提供最多3个库内中文候选供主人确认；"
        "不要把无法判断自动等同于混种。只能描述外观上的品种相似性，不能证明血统。"
        "外观字段只选下方appearance词表；无法看清留空，不按候选品种反推外观。"
        "年龄和体重无法可靠判断时必须降低 confidence 并可返回 null。"
        "词表：" + _json_dumps(catalog()) + "。只返回合法 JSON，结构为："
        + _json_dumps(schema)
    )
    data, model = vision_json(content, cfg, prompt, timeout=60.0)
    return _normalize_recognition(data), model


def vision_json(content, cfg, prompt, **options):
    image_url = "data:image/jpeg;base64," + base64.b64encode(content).decode("ascii")
    client = OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"], **options)
    response = client.chat.completions.create(
        model=cfg["model"],
        messages=[{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": image_url}},
            {"type": "text", "text": prompt},
        ]}],
        temperature=0,
    )
    raw = response.choices[0].message.content or ""
    return _parse_json_object(raw), cfg["model"]


def upload_and_recognize_pet(image_file: FileStorage, *, user_id: Any) -> dict[str, Any]:
    return upload_recognition(image_file, user_id, recognize_pet, 'recognition', PROMPT_VERSION)


def chat_photo_facts(user_id, conversation_id, attachments):
    if len(attachments) > 3:
        raise ValueError("每次最多发送三张照片")
    photos = []
    with _connect(autocommit=True) as conn, conn.cursor() as cursor:
        for item in attachments:
            image_id = item.get("image_id")
            if item.get("type") != "chat_image" or not re.fullmatch("[0-9a-f]{32}", str(image_id)):
                raise ValueError("图片附件无效")
            cursor.execute(f"""SELECT recognition_result_json FROM {TABLE_NAME}
                WHERE id=%s AND user_id=%s AND image_usage='guardian_chat' AND status='active'""",
                (image_id, user_id))
            row = cursor.fetchone()
            result = _json_loads((row or {}).get("recognition_result_json"), {})
            if result.get("conversation_id") != conversation_id:
                raise LookupError("会话图片不存在")
            photos.append(result.get("facts") or {})
    return photos


def upload_recognition(image_file, user_id, recognize, usage, version, *, conversation_id=None):
    cleaned_user_id = _clean(user_id, 128)
    if not cleaned_user_id:
        raise ValueError("user_id 不能为空")
    content, width, height = _prepare_image(image_file)
    init_pet_image_tables()
    image_id, now = uuid.uuid4().hex, _now()
    storage_path = _store_image(content, user_id=cleaned_user_id, image_id=image_id)
    digest = hashlib.sha256(content).hexdigest()
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""INSERT INTO {TABLE_NAME}
                (id,user_id,image_usage,original_filename,storage_path,content_type,file_size,sha256,width,height,
                 recognition_status,status,created_at,updated_at)
                VALUES (%s,%s,%s,%s,%s,'image/jpeg',%s,%s,%s,%s,'running','active',%s,%s)""",
                (image_id, cleaned_user_id, usage, _clean(image_file.filename, 512), storage_path, len(content), digest, width, height, now, now),
            )
        conn.commit()
    try:
        suggestions, model = recognize(content)
        recognition_status = "success" if suggestions else "failed"
        error = None
    except Exception as exc:
        suggestions, model = {}, os.getenv("QWEN_VISION_MODEL") or "qwen3-vl-plus"
        recognition_status, error = "failed", _clean(exc, 1000)
    stored = {"conversation_id": conversation_id, "facts": suggestions} if conversation_id else suggestions
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""UPDATE {TABLE_NAME} SET recognition_status=%s,recognition_result_json=%s,
                recognition_model=%s,prompt_version=%s,recognition_error=%s,updated_at=%s WHERE id=%s""",
                (recognition_status, _json_dumps(stored), model, version, error, _now(), image_id),
            )
        conn.commit()
    return {
        "ok": True,
        "image": {"id": image_id, "url": f"/api/miniprogram/pet-images/{image_id}", "width": width, "height": height},
        "recognition_status": recognition_status,
        "suggestions": suggestions,
        "recognition_error": error,
        "warnings": ["年龄和体重仅为照片估算，请按实际情况填写", "健康观察不构成疾病诊断"],
    }


def get_pet_image(image_id: Any, *, user_id: Any) -> dict[str, Any]:
    init_pet_image_tables()
    with _connect(autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"SELECT * FROM {TABLE_NAME} WHERE id=%s AND user_id=%s AND status='active' LIMIT 1",
                (_clean(image_id, 32), _clean(user_id, 128)),
            )
            row = cursor.fetchone()
    if not row:
        raise LookupError("宠物图片不存在")
    storage_path = str(row["storage_path"])
    if storage_path.startswith("oss://"):
        bucket_name, _, object_key = storage_path[6:].partition("/")
        config = _oss_config()
        expires = max(60, min(int(config["signed_url_expires"] or 3600), 604800))
        return {"redirect_url": _oss_bucket(config, bucket_name).sign_url("GET", object_key, expires, slash_safe=True), "content_type": row["content_type"], "sha256": row["sha256"]}
    path = Path(storage_path).resolve()
    try:
        path.relative_to((UPLOAD_DIR / "pets").resolve())
    except ValueError as exc:
        raise LookupError("宠物图片不存在") from exc
    if not path.is_file():
        raise LookupError("宠物图片不存在")
    return {"storage_path": path, "content_type": row["content_type"], "sha256": row["sha256"]}


def validate_pet_image_owner(image_id: Any, user_id: Any) -> None:
    get_pet_image(image_id, user_id=user_id)


def bind_pet_image(image_id: Any, *, user_id: Any, profile_id: Any) -> None:
    init_pet_image_tables()
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""UPDATE {TABLE_NAME} SET profile_id=%s,image_usage='avatar',updated_at=%s
                WHERE id=%s AND user_id=%s AND status='active'""",
                (_clean(profile_id, 32), _now(), _clean(image_id, 32), _clean(user_id, 128)),
            )
            if cursor.rowcount == 0:
                raise LookupError("宠物图片不存在")
        conn.commit()
