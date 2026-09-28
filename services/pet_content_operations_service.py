"""Operations workflow for pet materials, generated content drafts and publishing."""

from __future__ import annotations

import json
import os
import random
import uuid
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pymysql
import requests
from openai import OpenAI
from PIL import Image
from werkzeug.datastructures import FileStorage

from app_config import get_ark_image_config, get_mysql_config, get_qwen_config


MATERIAL_TABLE = "pet_material"
TASK_TABLE = "pet_content_task"
MAX_PAGE_SIZE = 100
GENERATED_DIR = Path(__file__).resolve().parents[1] / "var" / "generated_pet_content"
_generation_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pet-content")
OFFICIAL_USER_ID = os.getenv("MINIPROGRAM_OFFICIAL_USER_ID", "").strip() or "content-operations"
OFFICIAL_AUTHOR_NAME = "宠析官方"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _connect(autocommit: bool = False):
    return pymysql.connect(
        **get_mysql_config(database="csv_labeling"),
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=autocommit,
    )


def _clean(value: Any, limit: int | None = None) -> str:
    text = str(value or "").strip()
    return text[:limit] if limit else text


def _loads(value: Any, default: Any):
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return default


def init_pet_content_tables() -> None:
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"SHOW TABLES LIKE '{MATERIAL_TABLE}'")
            if not cursor.fetchone():
                raise RuntimeError("pet_material 表不存在，请先运行素材采集脚本")
            cursor.execute(f"SHOW COLUMNS FROM {MATERIAL_TABLE}")
            columns = {row["Field"] for row in cursor.fetchall() or []}
            additions = {
                "status": "VARCHAR(16) NOT NULL DEFAULT 'active' AFTER visual_tags",
                "review_status": "VARCHAR(16) NOT NULL DEFAULT 'pending' AFTER status",
                "used_count": "INT NOT NULL DEFAULT 0 AFTER review_status",
                "last_used_at": "DATETIME NULL AFTER used_count",
                "operator_note": "VARCHAR(1000) NULL AFTER last_used_at",
                "deleted_at": "DATETIME NULL AFTER operator_note",
            }
            for name, definition in additions.items():
                if name not in columns:
                    cursor.execute(f"ALTER TABLE {MATERIAL_TABLE} ADD COLUMN {name} {definition}")
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {TASK_TABLE} (
                    id CHAR(32) NOT NULL,
                    material_id BIGINT UNSIGNED NOT NULL,
                    image_prompt TEXT NOT NULL,
                    generation_model VARCHAR(64) NULL,
                    generation_task_id VARCHAR(128) NULL,
                    generated_image_url VARCHAR(1024) NULL,
                    generated_image_path VARCHAR(1024) NULL,
                    title VARCHAR(80) NULL,
                    content TEXT NULL,
                    hashtags_json JSON NULL,
                    status VARCHAR(32) NOT NULL DEFAULT 'pending',
                    publish_status VARCHAR(32) NOT NULL DEFAULT 'unpublished',
                    platform_post_id CHAR(32) NULL,
                    error_message TEXT NULL,
                    retry_count INT NOT NULL DEFAULT 0,
                    created_by VARCHAR(128) NULL,
                    reviewed_by VARCHAR(128) NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    published_at DATETIME NULL,
                    PRIMARY KEY (id),
                    KEY idx_pet_content_material (material_id,created_at),
                    KEY idx_pet_content_status (status,publish_status,created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
        conn.commit()


def _serialize_material(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["visual_tags"] = _loads(item.get("visual_tags"), [])
    return item


def _serialize_task(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["hashtags"] = _loads(item.pop("hashtags_json", None), [])
    return item


def list_materials(*, query: str = "", category: str = "", sub_category: str = "", review_status: str = "", page: Any = 1, page_size: Any = 24) -> dict[str, Any]:
    init_pet_content_tables()
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 24), MAX_PAGE_SIZE))
    where = ["status<>'deleted'"]
    params: list[Any] = []
    if query:
        where.append("(title LIKE %s OR search_keyword LIKE %s OR product_type LIKE %s)")
        like = f"%{_clean(query, 255)}%"
        params.extend([like, like, like])
    if category:
        where.append("category=%s")
        params.append(_clean(category, 64))
    if sub_category:
        where.append("sub_category=%s")
        params.append(_clean(sub_category, 128))
    if review_status:
        where.append("review_status=%s")
        params.append(_clean(review_status, 16))
    where_sql = " AND ".join(where)
    with _connect(autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) AS total FROM {MATERIAL_TABLE} WHERE {where_sql}", params)
            total = int(cursor.fetchone()["total"])
            cursor.execute(
                f"SELECT * FROM {MATERIAL_TABLE} WHERE {where_sql} ORDER BY created_at DESC,id DESC LIMIT %s OFFSET %s",
                [*params, page_size, (page - 1) * page_size],
            )
            rows = list(cursor.fetchall() or [])
            cursor.execute(f"SELECT DISTINCT category,sub_category FROM {MATERIAL_TABLE} WHERE status<>'deleted' ORDER BY category,sub_category")
            categories = list(cursor.fetchall() or [])
    return {"ok": True, "total": total, "page": page, "page_size": page_size, "items": [_serialize_material(r) for r in rows], "categories": categories}


def get_material(material_id: Any) -> dict[str, Any]:
    init_pet_content_tables()
    with _connect(autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"SELECT * FROM {MATERIAL_TABLE} WHERE id=%s AND status<>'deleted' LIMIT 1", (int(material_id),))
            row = cursor.fetchone()
    if not row:
        raise LookupError("素材不存在")
    return _serialize_material(row)


def update_material(material_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "category": 64, "sub_category": 128, "title": 1000, "product_type": 255,
        "main_subject": 500, "pet_action": 500, "interaction_type": 500,
        "usage_scene": 500, "visual_style": 500, "emotion": 500,
        "operator_note": 1000, "review_status": 16,
    }
    assignments, params = [], []
    for key, limit in allowed.items():
        if key in payload:
            value = _clean(payload.get(key), limit)
            if key == "review_status" and value not in {"pending", "approved", "rejected"}:
                raise ValueError("review_status 仅支持 pending/approved/rejected")
            assignments.append(f"{key}=%s")
            params.append(value or None)
    if not assignments:
        raise ValueError("没有可更新字段")
    params.append(int(material_id))
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"UPDATE {MATERIAL_TABLE} SET {','.join(assignments)} WHERE id=%s AND status<>'deleted'", params)
            if cursor.rowcount == 0:
                raise LookupError("素材不存在")
        conn.commit()
    return {"ok": True, "item": get_material(material_id)}


def delete_material(material_id: Any) -> dict[str, Any]:
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"UPDATE {MATERIAL_TABLE} SET status='deleted',deleted_at=%s WHERE id=%s AND status<>'deleted'", (_now(), int(material_id)))
            if cursor.rowcount == 0:
                raise LookupError("素材不存在")
        conn.commit()
    return {"ok": True, "id": int(material_id)}


CREATIVE_PLAN_SYSTEM_PROMPT = """你是一名宠物内容视觉策划和生图提示词专家。
根据输入的宠物素材分析结果，提炼核心创意并生成新的生图提示词。素材字段是事实依据，不得把产品类别、功能结构、宠物动作和互动机制改成别的东西。

优先级从高到低：
1. 准确保留产品/物体类别及实现功能所必需的可见结构；
2. 准确保留宠物与产品的互动机制、动作方向和因果关系；
3. 保留最核心的视觉亮点、情绪和使用场景；
4. 产品颜色、非功能造型、宠物品种、背景细节、机位和光线可以变化。

不得复制品牌、Logo、文字、水印、原图的独特产品外观或完全相同构图。不要凭空增加素材没有表达的产品功能，不要把互动产品弱化为普通摆件。
最终画面应为真实宠物摄影、自然生活方式、高质量商业摄影，有明显互动感和社交媒体传播感，竖版 3:4，单张完整画面，不要文字、Logo、水印、拼贴、边框。

严格返回 JSON，不要输出解释或 Markdown：
{"creative_core":"一句话核心创意","must_keep":["必须保留的信息"],"can_change":["允许变化的信息"],"final_prompt":"可直接用于中文生图模型的具体提示词"}
""".strip()

MATERIAL_PROMPT_FIELDS = (
    ("category", "大类"), ("sub_category", "细分类目"), ("product_type", "产品类型"),
    ("main_subject", "画面主体"), ("product_shape", "产品形状/功能结构"),
    ("main_colors", "主要颜色"), ("material_texture", "材质质感"),
    ("pet_type", "宠物类型"), ("pet_action", "宠物动作"),
    ("interaction_type", "互动机制"), ("usage_scene", "使用场景"),
    ("composition", "原构图信息"), ("camera_angle", "原镜头角度"),
    ("visual_style", "视觉风格"), ("emotion", "画面情绪"),
    ("standout_element", "核心视觉亮点"), ("visual_tags", "视觉标签"),
)


def _material_prompt_asset(material: dict[str, Any], style: str = "") -> dict[str, Any]:
    asset = {
        label: material.get(key)
        for key, label in MATERIAL_PROMPT_FIELDS
        if material.get(key) not in (None, "", [])
    }
    asset["新的创作方向"] = _clean(style, 100) or random.choice(
        ["真实宠物摄影", "自然生活方式摄影", "温馨治愈宠物摄影", "轻松幽默宠物摄影"]
    )
    return asset


def _fallback_creative_plan(material: dict[str, Any], style: str = "") -> dict[str, Any]:
    asset = _material_prompt_asset(material, style)
    product = _clean(material.get("product_type") or material.get("sub_category") or "宠物用品", 255)
    interaction = _clean(material.get("interaction_type") or material.get("pet_action") or "宠物自然地与产品互动", 500)
    structure = _clean(material.get("product_shape") or material.get("material_texture"), 500)
    highlight = _clean(material.get("standout_element") or material.get("emotion"), 500)
    scene = _clean(material.get("usage_scene") or "自然居家环境", 500)
    must_keep = [value for value in (f"产品明确是{product}", interaction, structure, highlight) if value]
    can_change = ["不影响功能的具体造型和颜色", "宠物品种", "背景细节", "拍摄角度和光线"]
    details = "；".join(f"{key}：{value}" for key, value in asset.items())
    final_prompt = (
        f"一张竖版 3:4 的真实宠物商业摄影照片。画面中产品必须清楚呈现为{product}，"
        f"宠物正在{interaction}，互动关系清晰、动作自然且安全。场景为{scene}。"
        + (f"产品可见结构与材质：{structure}。" if structure else "")
        + (f"核心视觉亮点：{highlight}。" if highlight else "")
        + f"参考信息：{details}。自然生活方式摄影，真实毛发与材质，高质量光影，主体完整，社交媒体传播感。"
    )
    return {"creative_core": f"宠物通过{interaction}使用{product}", "must_keep": must_keep, "can_change": can_change, "final_prompt": final_prompt}


def build_image_creative_plan(material: dict[str, Any], style: str = "", llm_client: Any = None) -> dict[str, Any]:
    """Use a text model to preserve material semantics before image generation."""
    fallback = _fallback_creative_plan(material, style)
    try:
        if llm_client is None:
            cfg = get_qwen_config({"model": "qwen-plus"})
            if not cfg["api_key"]:
                return fallback
            llm_client = OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"], timeout=60)
        user_prompt = "素材信息如下：\n" + json.dumps(_material_prompt_asset(material, style), ensure_ascii=False, indent=2)
        if hasattr(llm_client, "chat") and callable(llm_client.chat):
            raw = llm_client.chat(system_prompt=CREATIVE_PLAN_SYSTEM_PROMPT, user_prompt=user_prompt)
        else:
            response = llm_client.chat.completions.create(
                model=get_qwen_config({"model": "qwen-plus"})["model"], temperature=0.35,
                response_format={"type": "json_object"},
                messages=[{"role": "system", "content": CREATIVE_PLAN_SYSTEM_PROMPT}, {"role": "user", "content": user_prompt}],
            )
            raw = response.choices[0].message.content
        plan = json.loads(str(raw or "").strip())
        if not isinstance(plan, dict) or not _clean(plan.get("final_prompt"), 8000):
            return fallback
        must_keep = [_clean(value, 500) for value in plan.get("must_keep", []) if _clean(value, 500)]
        plan = {
            "creative_core": _clean(plan.get("creative_core"), 1000) or fallback["creative_core"],
            "must_keep": must_keep or fallback["must_keep"],
            "can_change": [_clean(value, 500) for value in plan.get("can_change", []) if _clean(value, 500)] or fallback["can_change"],
            "final_prompt": _clean(plan.get("final_prompt"), 8000),
        }
        return plan
    except Exception:
        return fallback


def build_image_prompt(
    material: dict[str, Any], style: str = "", llm_client: Any = None, *, enhance: bool = False,
) -> str:
    plan = (
        build_image_creative_plan(material, style, llm_client)
        if enhance or llm_client is not None
        else _fallback_creative_plan(material, style)
    )
    must_keep = "；".join(plan["must_keep"])
    can_change = "；".join(plan["can_change"])
    return (
        f"{plan['final_prompt']}\n\n"
        f"核心创意：{plan['creative_core']}。\n"
        f"必须准确保留：{must_keep}。\n"
        f"允许变化：{can_change}。\n"
        "硬性限制：不得改变产品类别或互动机制；不复制品牌、Logo、文字、水印或独特外观；"
        "不要文字、Logo、水印、拼贴、边框；单张完整画面；竖版 3:4。"
    ).strip()


def _create_task(material_id: int, prompt: str, created_by: str) -> str:
    task_id, now = uuid.uuid4().hex, _now()
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {TASK_TABLE}(id,material_id,image_prompt,status,publish_status,created_by,created_at,updated_at) VALUES(%s,%s,%s,'pending','unpublished',%s,%s,%s)",
                (task_id, material_id, prompt, created_by or None, now, now),
            )
        conn.commit()
    return task_id


def _update_task(task_id: str, **values: Any) -> None:
    if not values:
        return
    values["updated_at"] = _now()
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"UPDATE {TASK_TABLE} SET {','.join(f'{key}=%s' for key in values)} WHERE id=%s",
                [*values.values(), task_id],
            )
        conn.commit()


def _generate_image(prompt: str) -> tuple[str, str]:
    ark = get_ark_image_config()
    if not ark["api_key"]:
        raise RuntimeError("未配置 ARK_API_KEY")
    response = requests.post(
        f"{ark['base_url']}/images/generations",
        headers={"Authorization": f"Bearer {ark['api_key']}", "Content-Type": "application/json"},
        json={
            "model": ark["model"], "prompt": prompt, "size": "1728x2304",
            "sequential_image_generation": "disabled", "response_format": "url",
            "stream": False, "watermark": True,
        },
        timeout=180,
    )
    if response.status_code >= 400:
        try:
            body = response.json()
            error = body.get("error") or body.get("message") or body
        except (ValueError, AttributeError):
            error = response.text
        if isinstance(error, dict):
            error_code = _clean(error.get("code"), 128)
            error_message = _clean(error.get("message"), 1000)
        else:
            error_code = ""
            error_message = _clean(error, 1000)
        if error_code == "ModelNotOpen":
            raise RuntimeError(
                f"字节生图模型 {ark['model']} 尚未在当前火山方舟账号开通。"
                "请进入方舟控制台的“开通管理 > 模型 > 图片生成”开通该模型后重试。"
            )
        raise RuntimeError(
            f"字节生图失败{f'（{error_code}）' if error_code else ''}："
            f"{error_message or response.status_code}"
        )
    body = response.json()
    images = body.get("data") or []
    image_url = images[0].get("url") if images and isinstance(images[0], dict) else ""
    if not image_url:
        raise RuntimeError("字节生图成功但未返回图片 URL")
    request_id = response.headers.get("x-tt-logid") or str(body.get("created") or uuid.uuid4().hex)
    return request_id[:128], image_url


def _download_and_store(task_id: str, remote_url: str) -> tuple[str, str]:
    response = requests.get(remote_url, timeout=90)
    response.raise_for_status()
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    path = GENERATED_DIR / f"{task_id}.jpg"
    with Image.open(BytesIO(response.content)) as image:
        image.convert("RGB").save(path, format="JPEG", quality=92, optimize=True)
    from services.miniprogram_moment_image_service import upload_moment_image
    with path.open("rb") as stream:
        result = upload_moment_image(
            FileStorage(stream=stream, filename=path.name, content_type="image/jpeg"),
            user_id=OFFICIAL_USER_ID,
        )
    return result["item"]["url"], str(path)


def _fallback_copy(material: dict[str, Any]) -> dict[str, Any]:
    category = _clean(material.get("sub_category") or material.get("product_type") or "宠物玩具", 40)
    action = _clean(material.get("pet_action") or material.get("interaction_type") or "认真探索新玩具", 80)
    scene = _clean(material.get("usage_scene") or "家里", 40)
    return {
        "title": _clean(f"猫咪遇见{category}，好奇心藏不住了", 80),
        "content": _clean(
            f"今天在{scene}安排了一场小小的探索时间。猫咪正在{action}，从试探到投入，"
            "每一个动作都写满了好奇。合适的互动不仅能丰富日常，也让陪伴变得更有趣。"
            "你家猫咪面对新玩具时，是谨慎观察派，还是马上开玩派？",
            2000,
        ),
        "hashtags": ["猫咪", "养猫日常", category, "宠析"],
    }


def _generate_copy(material: dict[str, Any]) -> dict[str, Any]:
    fallback = _fallback_copy(material)
    qwen = get_qwen_config({"model": "qwen-plus"})
    if not qwen["api_key"]:
        return fallback
    try:
        client = OpenAI(api_key=qwen["api_key"], base_url=qwen["base_url"], timeout=60)
        response = client.chat.completions.create(
            model=qwen["model"], temperature=0.7, response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "你是宠物社区内容编辑。只返回JSON，字段为title、content、hashtags数组。标题不超过30字，正文80到180字，自然、真实、有互动问题，不宣称虚假功效。title和content不得为空。"},
                {"role": "user", "content": json.dumps({"category": material.get("sub_category"), "product_type": material.get("product_type"), "action": material.get("pet_action"), "scene": material.get("usage_scene"), "emotion": material.get("emotion")}, ensure_ascii=False)},
            ],
        )
        data = json.loads(response.choices[0].message.content or "{}")
        title = _clean(data.get("title"), 80)
        content = _clean(data.get("content"), 2000)
        hashtags = [_clean(x, 32) for x in (data.get("hashtags") or []) if _clean(x, 32)][:10]
        return {
            "title": title or fallback["title"],
            "content": content or fallback["content"],
            "hashtags": hashtags or fallback["hashtags"],
        }
    except Exception:
        return fallback


def _run_content_generation(task_id: str, material: dict[str, Any], style: str) -> None:
    try:
        prompt = build_image_prompt(material, style, enhance=True)
        _update_task(
            task_id, status="generating_image", image_prompt=prompt,
            generation_model=get_ark_image_config()["model"],
        )
        generation_task_id, remote_url = _generate_image(prompt)
        image_url, image_path = _download_and_store(task_id, remote_url)
        _update_task(task_id, status="generating_copy", generation_task_id=generation_task_id, generated_image_url=image_url, generated_image_path=image_path)
        copy = _generate_copy(material)
        _update_task(task_id, status="draft", title=copy["title"], content=copy["content"], hashtags_json=json.dumps(copy["hashtags"], ensure_ascii=False), error_message=None)
    except Exception as exc:
        _update_task(task_id, status="failed", error_message=_clean(exc, 4000))


def generate_content_task(material_id: Any, *, style: str = "", created_by: str = "admin") -> dict[str, Any]:
    """Persist and enqueue a task without blocking the HTTP request."""
    material = get_material(material_id)
    prompt = build_image_prompt(material, style)
    task_id = _create_task(int(material_id), prompt, _clean(created_by, 128))
    _generation_executor.submit(_run_content_generation, task_id, material, _clean(style, 100))
    return {"ok": True, "item": get_task(task_id)}


def list_tasks(*, status: str = "", limit: Any = 100) -> dict[str, Any]:
    init_pet_content_tables()
    where, params = [], []
    if status:
        where.append("t.status=%s")
        params.append(_clean(status, 32))
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    params.append(max(1, min(int(limit or 100), 200)))
    with _connect(autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"SELECT t.*,m.image_url AS material_image_url,m.title AS material_title,m.category,m.sub_category FROM {TASK_TABLE} t JOIN {MATERIAL_TABLE} m ON m.id=t.material_id {where_sql} ORDER BY t.created_at DESC LIMIT %s", params)
            rows = list(cursor.fetchall() or [])
    return {"ok": True, "count": len(rows), "items": [_serialize_task(r) for r in rows]}


def get_task(task_id: Any) -> dict[str, Any]:
    init_pet_content_tables()
    with _connect(autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"SELECT t.*,m.image_url AS material_image_url,m.title AS material_title,m.category,m.sub_category FROM {TASK_TABLE} t JOIN {MATERIAL_TABLE} m ON m.id=t.material_id WHERE t.id=%s LIMIT 1", (_clean(task_id, 32),))
            row = cursor.fetchone()
    if not row:
        raise LookupError("内容任务不存在")
    return _serialize_task(row)


def update_task_content(task_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    task = get_task(task_id)
    if task["publish_status"] == "published":
        raise ValueError("已发布内容不能修改")
    values: dict[str, Any] = {}
    for key, limit in (("image_prompt", 8000), ("title", 80), ("content", 2000)):
        if key in payload:
            values[key] = _clean(payload.get(key), limit)
    if "hashtags" in payload:
        if not isinstance(payload["hashtags"], list):
            raise ValueError("hashtags 必须是数组")
        values["hashtags_json"] = json.dumps([_clean(x, 32) for x in payload["hashtags"] if _clean(x, 32)][:10], ensure_ascii=False)
    if not values:
        raise ValueError("没有可更新字段")
    values["status"] = "draft"
    _update_task(_clean(task_id, 32), **values)
    return {"ok": True, "item": get_task(task_id)}


def approve_task(task_id: Any, reviewer: str = "admin") -> dict[str, Any]:
    task = get_task(task_id)
    if task["status"] not in {"draft", "approved"}:
        raise ValueError("只有草稿可以审核通过")
    if not _clean(task.get("title")) or not _clean(task.get("content")) or not task.get("generated_image_url"):
        raise ValueError("标题、正文和图片均不能为空")
    _update_task(_clean(task_id, 32), status="approved", reviewed_by=_clean(reviewer, 128))
    return {"ok": True, "item": get_task(task_id)}


def _run_image_regeneration(task: dict[str, Any]) -> None:
    try:
        _update_task(task["id"], generation_model=get_ark_image_config()["model"])
        generation_task_id, remote_url = _generate_image(task["image_prompt"])
        image_url, image_path = _download_and_store(task["id"], remote_url)
        _update_task(
            task["id"], status="draft", publish_status="unpublished",
            generation_task_id=generation_task_id, generated_image_url=image_url,
            generated_image_path=image_path, error_message=None,
        )
    except Exception as exc:
        _update_task(task["id"], status="failed", error_message=_clean(exc, 4000))


def regenerate_task_image(task_id: Any) -> dict[str, Any]:
    task = get_task(task_id)
    if task["publish_status"] == "published":
        raise ValueError("已发布内容不能重新生成图片")
    _update_task(task["id"], status="generating_image", error_message=None, retry_count=int(task.get("retry_count") or 0) + 1)
    task["status"] = "generating_image"
    _generation_executor.submit(_run_image_regeneration, task)
    return {"ok": True, "item": get_task(task_id)}


def publish_task(task_id: Any) -> dict[str, Any]:
    task = get_task(task_id)
    if task["publish_status"] == "published":
        return {"ok": True, "item": task}
    if task["status"] != "approved":
        raise ValueError("内容需要先审核通过")
    if not task.get("generated_image_url") or not _clean(task.get("title")) or not _clean(task.get("content")):
        raise ValueError("草稿缺少标题、正文或图片")
    hashtags = " ".join(f"#{tag.lstrip('#')}" for tag in task.get("hashtags") or [])
    from services.miniprogram_moment_service import create_moment
    _update_task(_clean(task_id, 32), status="publishing")
    try:
        result = create_moment({
            "user_id": OFFICIAL_USER_ID, "category_code": "FUNNY", "title": task.get("title"),
            "content": f"{task['content']}\n\n{hashtags}".strip(), "images": [{"url": task["generated_image_url"]}],
            "visibility": "public", "author_name": OFFICIAL_AUTHOR_NAME,
        })
        post_id = result["item"]["id"]
        now = _now()
        with _connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(f"UPDATE {TASK_TABLE} SET status='published',publish_status='published',platform_post_id=%s,published_at=%s,updated_at=%s WHERE id=%s", (post_id, now, now, task["id"]))
                cursor.execute(f"UPDATE {MATERIAL_TABLE} SET used_count=used_count+1,last_used_at=%s WHERE id=%s", (now, task["material_id"]))
            conn.commit()
    except Exception as exc:
        _update_task(task["id"], status="approved", publish_status="failed", error_message=_clean(exc, 4000))
        raise
    return {"ok": True, "item": get_task(task_id)}
