"""Operations workflow for pet materials, generated content drafts and publishing."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
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
COLLECTION_TASK_TABLE = "pet_material_collection_task"
MAX_PAGE_SIZE = 100
GENERATED_DIR = Path(__file__).resolve().parents[1] / "var" / "generated_pet_content"
_generation_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pet-content")
_collection_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pet-material-collection")
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
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {COLLECTION_TASK_TABLE} (
                    id CHAR(32) NOT NULL,
                    platform VARCHAR(32) NOT NULL,
                    category VARCHAR(64) NULL,
                    sub_category VARCHAR(128) NULL,
                    keyword VARCHAR(255) NULL,
                    item_limit INT NOT NULL DEFAULT 5,
                    use_vision TINYINT(1) NOT NULL DEFAULT 1,
                    status VARCHAR(32) NOT NULL DEFAULT 'pending',
                    collected_count INT NOT NULL DEFAULT 0,
                    log_text LONGTEXT NULL,
                    error_message TEXT NULL,
                    created_by VARCHAR(128) NULL,
                    created_at DATETIME NOT NULL,
                    started_at DATETIME NULL,
                    finished_at DATETIME NULL,
                    updated_at DATETIME NOT NULL,
                    PRIMARY KEY (id),
                    KEY idx_pet_material_collection_status (status,created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
        conn.commit()


def _serialize_collection_task(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["use_vision"] = bool(item.get("use_vision"))
    return item


def _build_collection_command(payload: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    platform = _clean(payload.get("platform") or "amazon", 32).lower()
    if platform not in {"amazon", "youtube", "facebook"}:
        raise ValueError("platform 仅支持 amazon/youtube/facebook")
    try:
        item_limit = int(payload.get("limit") or 5)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit 必须是整数") from exc
    if not 1 <= item_limit <= 20:
        raise ValueError("limit 必须在 1 到 20 之间")
    category = _clean(payload.get("category"), 64)
    sub_category = _clean(payload.get("sub_category"), 128)
    keyword = _clean(payload.get("keyword"), 255)
    use_vision = bool(payload.get("use_vision", True))
    script = Path(__file__).resolve().parents[1] / "scripts" / "pet_material_onefile.py"
    command = [sys.executable, "-u", str(script), "--platforms", platform, "--limit", str(item_limit)]
    if category:
        command.extend(["--category", category])
    if sub_category:
        command.extend(["--sub-category", sub_category])
    if keyword:
        command.extend(["--keyword", keyword])
    if not use_vision:
        command.append("--no-vision")
    return command, {
        "platform": platform, "category": category, "sub_category": sub_category,
        "keyword": keyword, "item_limit": item_limit, "use_vision": use_vision,
    }


def _update_collection_task(task_id: str, **values: Any) -> None:
    values["updated_at"] = _now()
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"UPDATE {COLLECTION_TASK_TABLE} SET {','.join(f'{key}=%s' for key in values)} WHERE id=%s",
                [*values.values(), task_id],
            )
        conn.commit()


def _run_collection_task(task_id: str, command: list[str]) -> None:
    _update_collection_task(task_id, status="running", started_at=_now(), error_message=None)
    try:
        result = subprocess.run(
            command, cwd=str(Path(__file__).resolve().parents[1]), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=1800, check=False,
        )
        output = _clean(result.stdout, 20000)
        match = re.search(r"完成，共处理\s*(\d+)\s*条素材", output)
        count = int(match.group(1)) if match else 0
        if result.returncode != 0:
            raise RuntimeError(output[-4000:] or f"采集进程退出码 {result.returncode}")
        _update_collection_task(
            task_id, status="completed", collected_count=count, log_text=output,
            error_message=None, finished_at=_now(),
        )
    except Exception as exc:
        _update_collection_task(
            task_id, status="failed", error_message=_clean(exc, 4000), finished_at=_now(),
        )


def start_material_collection(payload: dict[str, Any], created_by: str = "admin") -> dict[str, Any]:
    init_pet_content_tables()
    command, config = _build_collection_command(payload)
    task_id, now = uuid.uuid4().hex, _now()
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""INSERT INTO {COLLECTION_TASK_TABLE}
                (id,platform,category,sub_category,keyword,item_limit,use_vision,status,created_by,created_at,updated_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,'pending',%s,%s,%s)""",
                (task_id, config["platform"], config["category"] or None, config["sub_category"] or None,
                 config["keyword"] or None, config["item_limit"], int(config["use_vision"]),
                 _clean(created_by, 128) or None, now, now),
            )
        conn.commit()
    _collection_executor.submit(_run_collection_task, task_id, command)
    return {"ok": True, "item": get_material_collection_task(task_id)}


def get_material_collection_task(task_id: Any) -> dict[str, Any]:
    init_pet_content_tables()
    with _connect(autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"SELECT * FROM {COLLECTION_TASK_TABLE} WHERE id=%s LIMIT 1", (_clean(task_id, 32),))
            row = cursor.fetchone()
    if not row:
        raise LookupError("素材采集任务不存在")
    return _serialize_collection_task(row)


def list_material_collection_tasks(limit: Any = 20) -> dict[str, Any]:
    init_pet_content_tables()
    cleaned_limit = max(1, min(int(limit or 20), 100))
    with _connect(autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"SELECT * FROM {COLLECTION_TASK_TABLE} ORDER BY created_at DESC LIMIT %s",
                (cleaned_limit,),
            )
            rows = list(cursor.fetchall() or [])
    return {"ok": True, "count": len(rows), "items": [_serialize_collection_task(row) for row in rows]}


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

必须把创作要求拆成三层：
1. 共性层 common_layer：只写所有运营图都必须满足的最低标准，例如真实、安全、产品与互动清晰、竖版3:4、无文字。不要在这里固定猫的品种、地板、绿植、暖光或机位。
2. 差异层 difference_layer：写本图独有且必须被一眼看出的产品结构、互动动作、场景、构图、色调和情绪。至少包含一个与其他图明显不同的场景或镜头方案。
3. 互斥层 exclusion_layer：明确本图绝对不能出现的主体、产品形态、互动方式和模板化视觉组合，并禁止偏离指定视觉路线。

文本优先校正规则：原始产品标题、搜索关键词、人工类目中的明确产品定义，优先级高于图片视觉分析产生的模糊分类。视觉分析只能补充形状、材质和画面证据，不能把投食器改写成逗猫玩具，也不能用宠物当前动作反推错误的产品类别。
final_prompt 必须依次写清楚：产品主属性、功能机制、视觉证据、互斥限制，然后才能描述摄影风格。

不得复制品牌、Logo、文字、水印、原图的独特产品外观或完全相同构图。不要凭空增加素材没有表达的产品功能，不要把互动产品弱化为普通摆件。
避免反复使用“橘猫、浅木地板、窗边绿植、左侧暖光、居中构图”这一默认组合。除非输入明确要求，不要加入奶瓶、眼镜、梳子、遥控器等无关配件，也不要使用“亲子陪伴”等与宠物互动无关的叙事。

严格返回 JSON，不要输出解释或 Markdown：
{"common_layer":["最低共性要求"],"difference_layer":["本图独特要求"],"exclusion_layer":["本图禁止内容"],"final_prompt":"可直接用于中文生图模型的具体提示词"}
""".strip()

VISUAL_DIRECTIONS = (
    "低机位动态抓拍；冷白日光；深灰软垫或水泥质感地面；单只宠物；对角线构图；强调速度与瞬间动作",
    "俯拍观察视角；明亮中性色；纯色织物或地毯场景；留出大面积负空间；强调产品结构和互动路径",
    "近距离特写；深色简洁背景；侧逆光勾勒毛发和材质；浅景深；强调触碰细节，不出现完整居家陈设",
    "明快棚拍风格；高饱和但克制的双色背景；平视构图；清晰硬朗光影；强调产品轮廓与幽默表情",
    "户外或半户外自然环境；清晨散射光；石材或草地质感；广角环境构图；强调探索感和空间层次",
)

PHYSICAL_STRUCTURE_CONSTRAINT = (
    "所有物体必须符合真实重力、支撑、受力和机械连接关系。玩具主体必须通过地面、底座或素材中明确存在的结构稳定承重，不得悬空。"
    "如果素材包含弹簧、轮组、底座、透明罩、转轴、连杆或内部转动件，这些部件必须连续连接、装配位置合理，能够解释实际运动来源；"
    "不得为了表现运动而凭空增加素材没有的机械零件。猫只能接触现实中可触及的部位，猫爪、玩具和地面之间的距离、遮挡、接触点和运动方向必须自然。"
    "禁止漂浮、穿模、肢体或零件互相穿透、错误铰接、断裂连接、重复部件、无法承重、重心失衡和违反重力的姿态。"
    "最终产品必须像真实存在、可以制造并安全使用的宠物玩具，不得呈现为概念设计、魔法物体或超现实装置。"
)

MATERIAL_PROMPT_FIELDS = (
    ("title", "原始产品标题（仅用于识别类别，不复制品牌文案）"),
    ("search_keyword", "原始搜索关键词"),
    ("category", "大类"), ("sub_category", "细分类目"), ("product_type", "产品类型"),
    ("main_subject", "画面主体"), ("product_shape", "产品形状/功能结构"),
    ("main_colors", "主要颜色"), ("material_texture", "材质质感"),
    ("pet_type", "宠物类型"), ("pet_action", "宠物动作"),
    ("interaction_type", "互动机制"), ("usage_scene", "使用场景"),
    ("composition", "原构图信息"), ("camera_angle", "原镜头角度"),
    ("visual_style", "视觉风格"), ("emotion", "画面情绪"),
    ("standout_element", "核心视觉亮点"), ("visual_tags", "视觉标签"),
)

FEEDING_PRODUCT_TERMS = (
    "自动投食", "自动喂食", "零食发射", "零食投放", "投食器", "喂食器", "粮食分配",
    "treat dispenser", "treat launcher", "food dispenser", "automatic feeder", "snack dispenser",
)


def _product_semantic_brief(material: dict[str, Any]) -> dict[str, str]:
    text_evidence = " ".join(
        _clean(material.get(key), 1000)
        for key in ("title", "search_keyword", "sub_category", "product_type", "main_subject")
        if material.get(key)
    ).lower()
    if any(term in text_evidence for term in FEEDING_PRODUCT_TERMS):
        return {
            "primary_attribute": "本产品的核心类别是自动投食器 / 自动零食发射器，不是普通逗猫玩具",
            "mechanism": "产品应体现储存宠物零食、自动投放或发射零食的功能，运动和奖励必须由真实投食结构产生",
            "visual_evidence": "画面中应能看出粮仓或储粮空间、出粮口、可辨认的零食颗粒，以及猫靠近取食或等待投食的行为",
            "exclusion": "不要将其表现为追逐扑击类互动玩具，不要把重点放在猫追玩具，而应突出投食奖励机制；不得用羽毛、激光点或摆动逗猫杆代替出粮结构",
        }
    product = _clean(material.get("product_type") or material.get("sub_category") or "宠物用品", 255)
    mechanism = _clean(material.get("interaction_type") or material.get("pet_action") or "宠物按产品真实功能与其互动", 500)
    evidence = _clean(material.get("product_shape") or material.get("standout_element") or "画面应清楚呈现产品功能结构和宠物的实际接触点", 500)
    return {
        "primary_attribute": f"本产品的核心类别是{product}，不得改写成其他宠物用品",
        "mechanism": f"产品功能机制：{mechanism}",
        "visual_evidence": f"画面必须提供可验证产品类别和功能的视觉证据：{evidence}",
        "exclusion": f"不得把{product}表现成另一类产品，不得用与真实功能无关的追逐、扑击或装饰性动作取代核心机制",
    }


def _material_prompt_asset(material: dict[str, Any], style: str = "") -> dict[str, Any]:
    asset = {
        label: material.get(key)
        for key, label in MATERIAL_PROMPT_FIELDS
        if material.get(key) not in (None, "", [])
    }
    direction_index = int(material.get("id") or 0) % len(VISUAL_DIRECTIONS)
    asset["指定视觉路线"] = _clean(style, 100) or VISUAL_DIRECTIONS[direction_index]
    asset["文本优先产品校正"] = _product_semantic_brief(material)
    return asset


def _fallback_creative_plan(material: dict[str, Any], style: str = "") -> dict[str, Any]:
    asset = _material_prompt_asset(material, style)
    semantics = _product_semantic_brief(material)
    product = semantics["primary_attribute"]
    interaction = semantics["mechanism"]
    structure = _clean(material.get("product_shape") or material.get("material_texture"), 500)
    highlight = _clean(material.get("standout_element") or material.get("emotion"), 500)
    scene = _clean(material.get("usage_scene") or "符合指定视觉路线的简洁环境", 500)
    direction = asset["指定视觉路线"]
    common_layer = [
        "真实宠物商业摄影", "产品类别与互动机制清晰", "动作自然安全",
        "单张竖版 3:4 画面", "无文字、Logo、水印、拼贴和边框",
        PHYSICAL_STRUCTURE_CONSTRAINT,
    ]
    difference_layer = [
        semantics["primary_attribute"], semantics["mechanism"], semantics["visual_evidence"],
        structure, highlight, f"视觉路线：{direction}",
    ]
    exclusion_layer = [
        "不得改变产品类别或互动机制",
        "不得添加素材未提及的功能和无关配件",
        "不得使用橘猫、浅木地板、窗边绿植、左侧暖光、居中构图的模板化组合",
        "不得偏离指定视觉路线或复刻原素材构图",
        "不复制品牌、Logo、文字、水印或原素材的独特产品外观",
        "不得出现悬浮、穿模、错误铰接、断裂连接、重复肢体或违反重力的结构",
        semantics["exclusion"],
    ]
    details = "；".join(f"{key}：{value}" for key, value in asset.items())
    final_prompt = (
        f"【视觉路线】{direction}。一张竖版 3:4 的真实宠物商业摄影照片，互动因果清晰，场景为{scene}。"
        + (f"产品可见结构与材质：{structure}。" if structure else "")
        + (f"核心视觉亮点：{highlight}。" if highlight else "")
        + f"参考信息：{details}。自然生活方式摄影，真实毛发与材质，高质量光影，主体完整，社交媒体传播感。"
    )
    return {"common_layer": common_layer, "difference_layer": difference_layer, "exclusion_layer": exclusion_layer, "final_prompt": final_prompt}


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
        layers: dict[str, list[str]] = {}
        for key in ("common_layer", "difference_layer", "exclusion_layer"):
            model_values = [_clean(value, 500) for value in plan.get(key, []) if _clean(value, 500)]
            layers[key] = list(dict.fromkeys([*fallback[key], *model_values]))
        plan = layers | {
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
    semantics = _product_semantic_brief(material)
    semantic_block = (
        f"【产品主属性】{semantics['primary_attribute']}。\n"
        f"【功能机制】{semantics['mechanism']}。\n"
        f"【视觉证据】{semantics['visual_evidence']}。\n"
        f"【互斥限制】{semantics['exclusion']}。"
    )
    return (
        f"【文本优先校正】产品标题、搜索关键词和人工类目优先于图片视觉误判。\n"
        f"{semantic_block}\n\n{plan['final_prompt']}\n\n"
        f"共性层：{'；'.join(plan['common_layer'])}。\n"
        f"差异层：{'；'.join(plan['difference_layer'])}。\n"
        f"互斥层：{'；'.join(plan['exclusion_layer'])}。\n"
        f"【物理结构硬约束】{PHYSICAL_STRUCTURE_CONSTRAINT}"
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
