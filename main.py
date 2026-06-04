
from pathlib import Path
import atexit
import json
import os
import re
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

from flask import Flask, jsonify, redirect, request, send_from_directory
from werkzeug.utils import secure_filename
import yaml

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

try:
    from .api.consumer_api import consumer_api
    from .api.exception_api import exception_api
    from .api.orchestrator_api import orchestrator_api
    from .api.pipeline_api import pipeline_api
    from .api.product_identity_api import product_identity_api
    from .api.process_signal_api import process_signal_api
except ImportError:
    from api.consumer_api import consumer_api
    from api.exception_api import exception_api
    from api.orchestrator_api import orchestrator_api
    from api.pipeline_api import pipeline_api
    from api.product_identity_api import product_identity_api
    from api.process_signal_api import process_signal_api

from services import cat_food_task_service as task_store

try:
    from .services.orchestrator_service import create_task as create_orchestrator_task
    from .services.orchestrator_service import apply_node_result as apply_orchestrator_node_result
except ImportError:
    from services.orchestrator_service import create_task as create_orchestrator_task
    from services.orchestrator_service import apply_node_result as apply_orchestrator_node_result


WEB_DIR = Path(__file__).resolve().parent / "web"
DIST_DIR = BASE_DIR / "dist"
CONSUMER_APPS_DIR = BASE_DIR / "consumer_apps"
CATFOOD_BRAND_MASTER_PATH = BASE_DIR / "vendor" / "csv_mysql_labeling" / "config" / "catfood_brand_master.yaml"
_consumer_app_processes: list[subprocess.Popen] = []
_cat_food_task_executor = ThreadPoolExecutor(max_workers=2)


def _build_uploaded_image_filename(product_name: str, original_filename: str) -> str:
    original = secure_filename(original_filename or "") or "uploaded-image"
    suffix = Path(original).suffix.lower()
    if not suffix:
        suffix = ".jpg"

    product_slug = _safe_filename_stem(product_name)
    if not product_slug:
        product_slug = Path(original).stem or "uploaded-image"

    timestamp = datetime.now().strftime("%m%d%H%M%S")
    return f"{product_slug}_{timestamp}{suffix}"


def _safe_filename_stem(value: str, *, max_length: int = 80) -> str:
    stem = str(value or "").strip()
    if not stem:
        return ""
    stem = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", stem)
    stem = re.sub(r"\s+", "_", stem)
    stem = re.sub(r"_+", "_", stem).strip("._ ")
    return stem[:max_length].strip("._ ")


def _dedupe_storage_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"无法生成唯一上传文件名：{path.name}")


def _load_catfood_brand_options() -> list[str]:
    if not CATFOOD_BRAND_MASTER_PATH.exists():
        return []
    data = yaml.safe_load(CATFOOD_BRAND_MASTER_PATH.read_text(encoding="utf-8")) or {}
    brands = []
    seen = set()
    for row in data.get("brands") or []:
        if str(row.get("status") or "active").strip() != "active":
            continue
        name = str(row.get("standard_name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        brands.append(name)
    return brands


SUMMARY_PROMPT_VERSION = "cat-food-compare-summary-v6"


def _port_is_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex((host, port)) == 0


def _start_streamlit_app(script_name: str, port: int, base_url_path: str) -> None:
    if _port_is_open(port):
        return

    script_path = CONSUMER_APPS_DIR / script_name
    if not script_path.exists():
        return

    from app_config import get_feature_mysql_config

    db_config = get_feature_mysql_config()
    env = os.environ.copy()
    env.setdefault("MYSQL_HOST", str(db_config.get("host", "127.0.0.1")))
    env.setdefault("MYSQL_PORT", str(db_config.get("port", "3306")))
    env.setdefault("MYSQL_USER", str(db_config.get("user", "root")))
    env.setdefault("MYSQL_PASSWORD", str(db_config.get("password", "")))
    env.setdefault("MYSQL_DATABASE", str(db_config.get("database", "protein_feature_platform")))
    env.setdefault("MYSQL_CHARSET", str(db_config.get("charset", "utf8mb4")))

    cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(script_path),
        "--server.address",
        "127.0.0.1",
        "--server.port",
        str(port),
        "--server.headless",
        "true",
        "--server.baseUrlPath",
        base_url_path.strip("/"),
    ]
    process = subprocess.Popen(
        cmd,
        cwd=str(BASE_DIR),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    _consumer_app_processes.append(process)


def _start_consumer_apps() -> None:
    if os.getenv("CHONGXI_START_CONSUMER_APPS", "1").strip().lower() in {"0", "false", "no"}:
        return
    _start_streamlit_app("product_recommendation_engine.py", 8501, "consumer/recommendation-engine")
    _start_streamlit_app("product_compare_qwen.py", 8502, "consumer/product-compare")


def _stop_consumer_apps() -> None:
    for process in _consumer_app_processes:
        if process.poll() is None:
            process.terminate()


atexit.register(_stop_consumer_apps)


def _consumer_app_url(path: str, local_port: int) -> str:
    normalized_path = "/" + path.strip("/") + "/"
    hostname = request.host.split(":", 1)[0]
    if hostname in {"127.0.0.1", "localhost", "::1"}:
        return f"http://127.0.0.1:{local_port}{normalized_path}"
    return f"{request.scheme}://{hostname}{normalized_path}"


def _json_error(message: str, status: int = 400) -> tuple[Any, int]:
    return jsonify({"error": message}), status


def _friendly_score_from_risk(risk: dict) -> float | None:
    raw = risk.get("percentile")
    if raw in {None, "", "暂无"}:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 1:
        value *= 100
    return round(max(0.0, min(100.0, 100.0 - value)), 1)


def _friendly_level(score: float | None) -> str:
    if score is None:
        return "暂无数据"
    if score >= 75:
        return "高"
    if score >= 55:
        return "中等"
    if score >= 35:
        return "偏低"
    return "低"


def _friendly_position(score: float | None) -> str:
    if score is None:
        return "暂无数据"
    if score >= 75:
        return f"优于约{score:.0f}%的产品"
    if score >= 55:
        return "处于中游偏上"
    if score >= 35:
        return "低于多数产品"
    return "处于靠后位置"


def _friendly_display(score: float | None) -> str:
    if score is None:
        return "暂无数据"
    return f"{score:.1f}｜{_friendly_level(score)}｜{_friendly_position(score)}"


def _product_full_name(product: dict) -> str:
    return " ".join(
        part
        for part in [
            str(product.get("brand_name") or "").strip(),
            str(product.get("name") or "").strip(),
        ]
        if part
    ) or str(product.get("name") or "").strip() or "该产品"


def _friendly_interpretation(
    dimension: str,
    current_score: float | None,
    target_score: float | None,
    current_name: str,
    target_name: str,
) -> str:
    if current_score is None or target_score is None:
        return f"暂不支持{dimension}横向评估"
    delta = current_score - target_score
    if abs(delta) < 8:
        return f"{current_name} 和 {target_name} 在{dimension}上接近，建议结合猫咪体质和其他画像维度判断"
    if delta > 0:
        return f"{current_name} 在产品库中更靠前，{dimension}优势更明显"
    return f"{target_name} 在产品库中更靠前，{dimension}优势更明显"


def _build_friendly_rows(current_product: dict, target_product: dict) -> list[dict]:
    black_current = _friendly_score_from_risk(current_product.get("black_chin_risk", {}) or {})
    black_target = _friendly_score_from_risk(target_product.get("black_chin_risk", {}) or {})
    gut_current = _friendly_score_from_risk(current_product.get("soft_stool_risk", {}) or {})
    gut_target = _friendly_score_from_risk(target_product.get("soft_stool_risk", {}) or {})
    current_name = _product_full_name(current_product)
    target_name = _product_full_name(target_product)
    return [
        {
            "dimension": "黑下巴友好度",
            "current": _friendly_display(black_current),
            "target": _friendly_display(black_target),
            "interpretation": _friendly_interpretation("黑下巴友好度", black_current, black_target, current_name, target_name),
            "current_score": black_current,
            "target_score": black_target,
        },
        {
            "dimension": "肠胃友好度",
            "current": _friendly_display(gut_current),
            "target": _friendly_display(gut_target),
            "interpretation": _friendly_interpretation("肠胃友好度", gut_current, gut_target, current_name, target_name),
            "current_score": gut_current,
            "target_score": gut_target,
        },
        {
            "dimension": "适口友好度",
            "current": "暂无数据",
            "target": "暂无数据",
            "interpretation": "暂不支持适口性横向评估",
            "current_score": None,
            "target_score": None,
        },
    ]


def _profile_records(product: dict) -> list[dict]:
    records = []
    for row in product["profile_df"].to_dict(orient="records"):
        records.append(
            {
                "dimension": row.get("dimension"),
                "score": row.get("score"),
                "level": row.get("level"),
                "type": row.get("type"),
                "summary": row.get("summary"),
            }
        )
    return records


def _baseline_records(product: dict) -> list[dict]:
    baseline_df = product.get("baseline_df")
    if baseline_df is None or baseline_df.empty:
        return []
    return [
        {
            "dimension": row.get("dimension"),
            "score": row.get("score"),
        }
        for row in baseline_df.to_dict(orient="records")
    ]


def _load_compare_app():
    try:
        from .consumer_apps import product_compare_qwen as compare_app
    except ImportError:
        from consumer_apps import product_compare_qwen as compare_app
    return compare_app


def _build_cat_food_compare_response(payload: dict) -> dict:
    current_query = str(payload.get("current_food") or "").strip()
    target_query = str(payload.get("target_food") or "").strip()
    if not current_query or not target_query:
        raise ValueError("请选择当前粮和对比粮。")
    if current_query == target_query:
        raise ValueError("当前粮和对比粮不能相同。")

    compare_app = _load_compare_app()
    current_product = compare_app.build_product_context(current_query)
    target_product = compare_app.build_product_context(target_query)
    diff_df = compare_app.build_profile_diff(current_product, target_product)
    core_diff_explanations = compare_app.build_core_diff_explanations(
        diff_df,
        current_product["name"],
        target_product["name"],
    )
    tag_diff_summary = compare_app.build_tag_diff_summary(current_product, target_product)
    need_focus_df = compare_app.build_need_focus_table(
        diff_df,
        current_product["name"],
        target_product["name"],
    )
    llm_context = compare_app.build_llm_compare_context(
        product_a=current_product,
        product_b=target_product,
        diff_df=diff_df,
        core_diff_explanations=core_diff_explanations,
        tag_diff_summary=tag_diff_summary,
        need_focus_df=need_focus_df,
    )
    return {
        "current_food": {
            "query": current_query,
            "name": current_product["name"],
            "brand_name": current_product["brand_name"],
            "ingredient_composition": current_product.get("ingredient_composition") or "",
            "profile": _profile_records(current_product),
            "baseline_profile": _baseline_records(current_product),
        },
        "target_food": {
            "query": target_query,
            "name": target_product["name"],
            "brand_name": target_product["brand_name"],
            "ingredient_composition": target_product.get("ingredient_composition") or "",
            "profile": _profile_records(target_product),
            "baseline_profile": _baseline_records(target_product),
        },
        "profile_diff": compare_app.make_json_safe(diff_df.to_dict(orient="records")),
        "friendly_rows": _build_friendly_rows(current_product, target_product),
        "core_diff_explanations": compare_app.make_json_safe(core_diff_explanations),
        "tag_diff_summary": compare_app.make_json_safe(tag_diff_summary),
        "llm_context": compare_app.make_json_safe(llm_context),
    }


def _summary_cache_key(context: dict, friendly_rows: list) -> tuple[str, str]:
    product_a = context.get("product_a", {}) if isinstance(context, dict) else {}
    product_b = context.get("product_b", {}) if isinstance(context, dict) else {}
    payload = {
        "prompt_version": SUMMARY_PROMPT_VERSION,
        "product_a": {
            "brand_name": product_a.get("brand_name"),
            "name": product_a.get("name"),
            "profile": product_a.get("profile"),
            "black_chin_risk": product_a.get("black_chin_risk"),
            "soft_stool_risk": product_a.get("soft_stool_risk"),
        },
        "product_b": {
            "brand_name": product_b.get("brand_name"),
            "name": product_b.get("name"),
            "profile": product_b.get("profile"),
            "black_chin_risk": product_b.get("black_chin_risk"),
            "soft_stool_risk": product_b.get("soft_stool_risk"),
        },
        "profile_diff": context.get("profile_diff") if isinstance(context, dict) else None,
        "tag_diff_summary": context.get("tag_diff_summary") if isinstance(context, dict) else None,
        "friendly_rows": friendly_rows,
    }
    input_hash = task_store.stable_hash(payload)
    return f"{SUMMARY_PROMPT_VERSION}:{input_hash}", input_hash


def _score_reading_notes(context: dict) -> list[str]:
    if not isinstance(context, dict):
        return []
    product_a = context.get("product_a", {}) if isinstance(context.get("product_a"), dict) else {}
    product_b = context.get("product_b", {}) if isinstance(context.get("product_b"), dict) else {}
    product_a_name = " ".join(
        part for part in [str(product_a.get("brand_name") or "").strip(), str(product_a.get("name") or "").strip()] if part
    ) or "产品A"
    product_b_name = " ".join(
        part for part in [str(product_b.get("brand_name") or "").strip(), str(product_b.get("name") or "").strip()] if part
    ) or "产品B"

    notes = []
    for row in context.get("profile_diff") or []:
        if not isinstance(row, dict):
            continue
        dimension = row.get("dimension")
        product_a_score = row.get("product_a_score")
        product_b_score = row.get("product_b_score")
        diff = row.get("diff_b_minus_a")
        score_type = row.get("type")
        if product_a_score is None or product_b_score is None:
            continue
        note = (
            f"{dimension}: {product_a_name}={product_a_score}, {product_b_name}={product_b_score}; "
            f"diff_b_minus_a={diff} 只表示{product_b_name}减{product_a_name}的差值。"
        )
        if score_type == "protective":
            note += " 这是保护/支持型指标，分数越高通常支持越强。"
        elif score_type == "pressure":
            note += " 这是压力/负担型指标，分数越低通常压力越轻。"
        notes.append(note)
    return notes


def _compact_product_name(product: dict, fallback: str) -> str:
    return " ".join(
        part for part in [str(product.get("brand_name") or "").strip(), str(product.get("name") or "").strip()] if part
    ) or fallback


def _extract_chart_structure_scores(context: dict) -> dict[str, Any]:
    if not isinstance(context, dict):
        return {}
    products = {
        "current_food": context.get("product_a", {}) if isinstance(context.get("product_a"), dict) else {},
        "target_food": context.get("product_b", {}) if isinstance(context.get("product_b"), dict) else {},
    }
    result: dict[str, Any] = {}
    for key, product in products.items():
        result[key] = {
            "product_name": _compact_product_name(product, key),
            "scores": [
                {
                    "dimension": row.get("dimension"),
                    "score": row.get("score"),
                    "type": row.get("type"),
                    "meaning": row.get("meaning") or row.get("summary"),
                }
                for row in product.get("profile") or []
                if isinstance(row, dict)
            ],
        }
    return result


def _extract_risk_tags(context: dict) -> dict[str, Any]:
    if not isinstance(context, dict):
        return {}
    products = {
        "current_food": context.get("product_a", {}) if isinstance(context.get("product_a"), dict) else {},
        "target_food": context.get("product_b", {}) if isinstance(context.get("product_b"), dict) else {},
    }
    risk_keys = {
        "black_chin_risk": "黑下巴风险",
        "soft_stool_risk": "软便风险",
    }
    result: dict[str, Any] = {}
    for product_key, product in products.items():
        product_risks = {
            "product_name": _compact_product_name(product, product_key),
            "risks": {},
        }
        for risk_key, risk_name in risk_keys.items():
            risk = product.get(risk_key) if isinstance(product.get(risk_key), dict) else {}
            product_risks["risks"][risk_key] = {
                "risk_name": risk_name,
                "risk_level": risk.get("risk_level"),
                "relative_position": risk.get("relative_position"),
                "percentile": risk.get("percentile"),
                "tags": risk.get("tags") or [],
            }
        result[product_key] = product_risks
    return result


def _generate_cat_food_summary(payload: dict) -> dict:
    context = payload.get("llm_context")
    friendly_rows = payload.get("friendly_rows") or []
    cat_profile = payload.get("cat_profile") or {}
    if not context:
        raise ValueError("缺少大模型上下文。")

    compare_app = _load_compare_app()
    product_a = context.get("product_a", {}) if isinstance(context, dict) else {}
    product_b = context.get("product_b", {}) if isinstance(context, dict) else {}
    product_a_full_name = " ".join(
        part for part in [str(product_a.get("brand_name") or "").strip(), str(product_a.get("name") or "").strip()] if part
    ) or "产品A"
    product_b_full_name = " ".join(
        part for part in [str(product_b.get("brand_name") or "").strip(), str(product_b.get("name") or "").strip()] if part
    ) or "产品B"
    cache_key, input_hash = _summary_cache_key(context, friendly_rows)
    cached = task_store.get_cached_llm_result(cache_key)
    if cached:
        return {"summary": cached["result_text"], "cached": True, "cache_key": cache_key}

    enriched_context = {
        "comparison_mode": "当前粮到对比粮的换粮判断",
        "cat_profile": cat_profile,
        "chart_structure_scores": _extract_chart_structure_scores(context),
        "friendliness_scores": friendly_rows,
        "risk_tags": _extract_risk_tags(context),
        "score_reading_notes": _score_reading_notes(context),
        "base_context": context,
    }
    system_prompt = """
你是宠物食品配方对比解释助手，负责基于两款猫粮的结构分、友好度分和风险标签，生成面向普通宠主的对比卡片文案。

你的输出风格应该：
- 像宠物食品数据助手，不像技术报告；
- 口语化、清楚、有一点轻松感；
- 可以使用少量宠主容易理解的表达，如“偏科”“玻璃胃”“长肉”“保肌肉”“下巴出油”“换粮要慢慢来”；
- 但不能夸张、恐吓或绝对化；
- 必须严格基于输入数据，不得引入未提供的品牌口碑、用户评价、价格、适口性或外部资料。

你的核心任务不是判断哪款粮绝对更好，而是帮助用户理解：
1. 本次对比中，哪款在什么方向更占优势；
2. 哪些只是“相对更好”，不能被理解成真正友好；
3. 哪些指标虽然高，但在本次对比里不是优势方；
4. 什么猫可以考虑，什么猫要谨慎；
5. 换粮后重点观察什么。

====================
【输入信息】
====================

你会收到两款猫粮的数据：

1. 当前粮：
- 产品名
- 图1结构分：
  - 蛋白质量
  - 蛋白压力
  - 碳水负担
  - 脂肪负担
  - 纤维缓冲
  - 菌群支持
  - 皮肤保护
- 图2友好度分：
  - 黑下巴友好度
  - 肠胃友好度
  - 适口友好度，如有
- 风险标签：
  - 黑下巴风险等级
  - 黑下巴风险原因标签
  - 软便风险等级
  - 软便风险原因标签

2. 对比粮：
- 产品名
- 图1结构分
- 图2友好度分
- 风险标签

3. 用户背景，如有：
- 猫龄
- 历史问题
- 近期症状
- 换粮目标

如果用户背景为空，按“无明显历史问题、无明显近期症状”处理，但不得默认推荐某一款长期喂养。

====================
【第一步：先判断本次对比优势方】
====================

在生成文案前，必须先进行内部判断，但不要展示判断过程。

你必须先判断每个方向的“本次对比优势方”：

1. 蛋白/保肌肉方向：
综合查看：
- 蛋白质量：越高越好；
- 蛋白压力：越低越好；
- 软便风险：越低越好；
- 肠胃友好度、纤维缓冲、菌群支持：作为限制项。

判断规则：
- 如果某款粮蛋白质量更高，且蛋白压力更低，则该款粮应被判断为“蛋白方向优势方”。
- 另一款即使蛋白质量本身也高，也只能写成“蛋白质量不低”，不能写成“本次对比中的保肌肉优势方”。
- 如果某款蛋白质量更高但软便风险、脂肪负担或肠道支撑存在明显短板，必须写成“蛋白方向更突出，但不等于适合所有猫长期稳定喂”。

示例：
当前粮蛋白质量85.5，对比粮蛋白质量95.1；当前粮蛋白压力42.9，对比粮蛋白压力28.8。
必须写：
“皇家 BK34 蛋白质量不低，但本次对比里，百利高蛋白美毛的蛋白质量更高、蛋白压力更低，蛋白/保肌肉方向更占优势。”

不能写：
“皇家 BK34 更适合保肌肉。”
“皇家 BK34 是长肉能手。”
除非当前粮在蛋白质量和蛋白压力上都优于对比粮。

2. 黑下巴方向：
综合查看：
- 黑下巴友好度：越高越好；
- 黑下巴风险等级：越低越好；
- 脂肪负担、皮肤保护、脂肪调节相关标签：作为辅助判断。

判断规则：
- 黑下巴友好度低于40时，不能写“黑下巴友好”。
- 风险等级为“高”或“中高”时，不能写“低风险”。
- 只能写“黑下巴压力相对低一些”或“仍需观察”。

3. 肠胃/软便方向：
综合查看：
- 肠胃友好度：越高越好；
- 软便风险等级：越低越好；
- 蛋白压力、碳水负担：越低越好；
- 纤维缓冲、菌群支持：越高越好。

判断规则：
- 肠胃友好度低于40时，不能写“肠胃友好”或“适合玻璃胃”。
- 纤维缓冲或菌群支持为0时，必须提示“便便成形和肠道支撑短板明显”。
- 蛋白压力低、碳水负担低，只能说明“消化适应压力可能相对低一些”，不能直接写“好消化”。

4. 皮肤保护方向：
综合查看：
- 皮肤保护分；
- 黑下巴风险；
- 脂肪负担；
- Omega、抗氧化、脂肪调节相关标签。

判断规则：
- 皮肤保护分低于40时，不能写“皮肤友好”或“护肤能力强”。
- 只能写“皮肤保护相对更高，但整体仍不算强”。

====================
【核心判断规则】
====================

1. 必须区分“相对更好”和“真的适合”。

如果某款粮在某个维度高于另一款，但该维度绝对分低于40，只能说：
- 相对更高
- 相对没那么差
- 压力相对低一些
- 可以作为观察方向
- 仍需观察

不能说：
- 友好
- 适合
- 轻松
- 优秀
- 放心
- 明显改善

2. 如果某个维度低于20，必须提示“整体仍偏弱”。

示例：
纤维缓冲 12，高于另一款 0。
可以说：
“皇家 BK34 的纤维缓冲比百利好一些，但12分仍偏弱。”

不能说：
“皇家 BK34 肠道保护更强。”

3. 如果两款在某个维度都低于40，不得把较高的一款写成该方向推荐。

只能写：
“相对更高，但整体都不算强。”
“这一项两款都需要观察。”

4. 风险标签为“高”或“中高”时，禁止输出：
- 低风险
- 放心换
- 长期稳定优选
- 默认推荐
- 闭眼选

5. 只有一个证据支持的结论，只能写成“观察方向”，不能写成“推荐”。

6. 任何“更适合 / 可以优先考虑 / 更建议选”必须至少有两个证据来源支持。

证据来源包括：
- 图1结构分；
- 图2友好度分；
- 风险标签。

7. 如果图1结构分、图2友好度分和风险标签之间存在冲突，必须保守表达，并加入“仍需观察”。

示例：
某款粮软便风险标签不高，但肠胃友好度低、纤维缓冲低、菌群支持低。
不能说：
“适合肠胃敏感猫。”
“好消化。”

应该说：
“蛋白/碳水压力相对低一些，但肠道支撑偏弱，便便成形仍需观察。”

8. 蛋白质量和蛋白压力必须分开解释。

- 蛋白质量高：可以解释为“更偏营养密度、肌肉维持、长结实”。
- 蛋白压力高：可以解释为“更考验消化适应”。
- 蛋白压力低：只能解释为“消化负担可能相对低一些”，不能直接写成“好消化”。

9. 黑下巴和皮肤保护必须分开解释。

- 黑下巴友好度不等于整体皮肤保护。
- 皮肤保护高于另一款，不等于强皮肤护理。
- 如果皮肤保护低于40，只能说“相对好一些”，不能说“护肤能力强”。

10. 软便风险和肠胃友好度必须分开解释。

- 软便风险低不等于肠胃友好。
- 肠胃友好度高于另一款，不等于适合玻璃胃。
- 判断肠胃方向必须同时看：
  - 肠胃友好度
  - 软便风险标签
  - 蛋白压力
  - 碳水负担
  - 纤维缓冲
  - 菌群支持

11. 不要把“没有某些风险标签”写成“没有风险”。

只能写：
“该项标签压力相对少一些。”
“但仍需结合分数观察。”

====================
【A/B对比优先规则】
====================

1. 文案必须优先表达“本次对比谁在该方向更占优势”，不能只描述当前粮自身分数。

错误示例：
“皇家 BK34 蛋白质量85.5，适合保肌肉。”

如果对比粮蛋白质量更高、蛋白压力更低，必须改为：
“皇家 BK34 蛋白质量不低，但本次对比里，百利在蛋白质量和蛋白压力上更占优势。”

2. 当当前粮某项分数较高，但对比粮更高时，当前粮只能写“本身不低”，不能写成“优势”。

示例：
当前粮蛋白质量85.5，对比粮95.1。
当前粮不能写：
“蛋白质量是优势。”

应该写：
“蛋白质量本身不低，但对比粮更高。”

3. 当某款粮同时满足“关键收益指标更高”和“关键压力指标更低”时，应优先识别为该方向优势方。

示例：
蛋白方向：
- 蛋白质量更高；
- 蛋白压力更低。
则该款为蛋白方向优势方。

消化压力方向：
- 蛋白压力更低；
- 碳水负担更低；
- 但如果纤维缓冲或菌群支持很低，必须补充“肠道支撑短板”。

4. 不允许因为当前粮是“用户正在吃的粮”，就默认给当前粮更多正向描述。

当前粮和对比粮必须按同一标准判断。

5. “继续观察当前粮”只能在以下情况下出现：
- 当前粮在用户目标相关维度不明显劣于对比粮；
- 当前粮没有与用户历史问题强冲突的高风险标签；
- 文案同时提示需要观察风险。

否则不能为了稳妥而默认建议继续当前粮。

====================
【语气与措辞规则】
====================

允许使用轻量口语化表达：
- 有点偏科
- 比较考验肠胃
- 玻璃胃要谨慎
- 长肉/保肌肉方向更突出
- 下巴出油要重点看
- 换粮要慢慢来
- 不是闭眼选项
- 不算理想首选

禁止使用过度绝对或恐吓表达：
- 极易引发
- 风险拉满
- 必然软便
- 一定黑下巴
- 绝对不能吃
- 毒粮
- 完全不行
- 闭眼冲
- 无脑选

把强表达替换为保守表达：
- “极易引发黑下巴” → “黑下巴压力较高，需要重点观察”
- “好消化” → “蛋白/碳水压力相对低一些”
- “长肉效果好” → “更偏向蛋白质量和肌肉维持”
- “缺乏肠道保护成分” → “纤维缓冲和菌群支持偏弱”
- “适合玻璃胃” → “玻璃胃需要谨慎，不应只看低蛋白压力”

====================
【输出风格要求】
====================

请生成面向普通宠主的对比卡片文案。

要求：
1. 总字数控制在450–650字；
2. 可以使用标题、emoji和简短小标签；
3. 不要写成技术审计报告；
4. 不要复述所有指标；
5. 每款粮最多写2个优势、2个风险；
6. 每个自然段最多出现3个数字；
7. 分数最多保留1位小数；
8. 不输出百分位和排名，除非它们是唯一证据；
9. 保留关键证据，但不要堆数字；
10. 结论必须是条件式，不得写默认推荐；
11. 必须体现A/B对比，不得只围绕当前粮展开。

====================
【必须输出结构】
====================

请严格按以下结构输出：

【一句话总结：用一句有记忆点的小标题】
要求：
- 小标题可以轻松一点；
- 正文用2–3句话说明两款粮的核心取舍；
- 必须说清楚：
  - 当前粮主要强项；
  - 对比粮主要强项；
  - 两款共同需要谨慎的地方；
- 不要超过4个数字。

【🥩 当前粮怎么看？】
包含：
- 一个轻量标签，例如“蛋白质量不低，但不是本次蛋白优势方”“相对均衡但肠胃要观察”“更考验肠胃”；
- 1–2个优势；
- 1–2个风险；
- 至少引用1个关键分数或风险标签；
- 如果某项只是相对更好但绝对分低，必须说明“不算真正友好”；
- 如果对比粮在某个核心方向明显更强，必须明确承认当前粮不是该方向优势方。

【🍗 对比粮怎么看？】
包含：
- 一个轻量标签，例如“蛋白方向更突出”“压力更低但偏科”“黑下巴要盯紧”；
- 1–2个优势；
- 1–2个风险；
- 至少引用1个关键分数或风险标签；
- 如果纤维缓冲或菌群支持为0，必须明确提示“便便成形和肠道支撑短板明显”；
- 如果该粮是本次蛋白方向优势方，必须说明“蛋白质量更高 + 蛋白压力更低”这两个证据。

【🤔 到底怎么选？】
用条件式建议，不得写成绝对推荐。
必须包含：
- 如果更看重某个目标，可以偏向哪款；
- 如果猫咪已有某类问题，哪款需要谨慎；
- 如果两款都不是某类猫的理想首选，必须明确说明。

推荐句式：
“如果你家猫……，可以优先观察……”
“如果猫咪已经有……，这两款都不算理想首选。”

【⚠️ 换粮期观察】
包含：
- 建议10–14天渐进式换粮；
- 观察3类重点现象；
- 出现连续异常时建议暂停换粮并咨询兽医。

优先选择观察项：
- 便便成形
- 软便/腹泻
- 呕吐
- 食欲变化
- 下巴出油
- 黑下巴
- 毛发状态
- 皮肤瘙痒

====================
【禁止表达】
====================

禁止输出以下表达：

- 默认推荐长期喂养
- 综合更好
- 完全优于
- 闭眼选
- 无脑选
- 可以放心换
- 肠胃轻松
- 肠胃友好型
- 黑下巴友好型
- 皮肤友好型
- 明显改善
- 长期稳定优选
- 更适合所有猫
- 极易引发
- 风险拉满
- 必然软便
- 一定黑下巴
- 毒粮

如需表达倾向，请使用：
- 相对更有优势
- 可以作为观察方向
- 更值得关注
- 对某类压力可能更低
- 但仍需观察
- 不应理解为完全友好
- 不算理想首选

====================
【输出前自检】
====================

输出前请进行内部自检，但不要展示自检过程：

1. 是否先判断了本次对比的优势方？
如果没有，必须先判断再写文案。

2. 是否把“当前粮自身分数高”误写成“当前粮是优势方”？
如果对比粮更高，必须改成“当前粮本身不低，但对比粮更占优势”。

3. 是否把蛋白质量高但蛋白压力也高的产品写成了“保肌肉优选”？
如果是，必须结合对比粮的蛋白质量和蛋白压力重新判断。

4. 是否把低于40分的指标写成了“友好/适合/推荐”？
如果是，改成“相对更高，但整体仍偏弱”。

5. 是否把“黑下巴友好度高于另一款”写成了“黑下巴友好”？
如果是，改成“黑下巴压力相对低一些，但不算真正友好”。

6. 是否把“蛋白压力低、碳水负担低”写成了“好消化”？
如果是，改成“消化适应压力可能相对低一些”。

7. 是否把“纤维缓冲和菌群支持低”写成了“缺少益生菌/膳食纤维”？
如果输入没有明确成分，不要这么写，统一写成“肠道支撑偏弱”。

8. 是否只根据一个指标给出选择建议？
如果是，改成“观察方向”。

9. 是否输出了“默认推荐、综合更好、放心换、风险拉满、极易引发”等强表达？
如果是，删除或改成条件式、保守表达。

10. 是否整体太像技术报告？
如果是，减少数字，保留核心证据，用更自然的宠主语言改写。
"""
    client = compare_app.get_qwen_client()
    completion = client.chat.completions.create(
        model=compare_app.QWEN_CONFIG["model"],
        messages=[
            {
                "role": "system",
                "content": system_prompt.strip(),
            },
            {
                "role": "user",
                "content": (
                    "请只基于下面结构化数据输出，不要编造成分、疾病诊断或输入中不存在的结论。"
                    f"产品A完整名称是：{product_a_full_name}。产品B完整名称是：{product_b_full_name}。"
                    "请严格按 system 中指定格式输出，并把模板中的产品A完整品牌+产品名称、产品B完整品牌+产品名称替换成上述完整名称。\n\n"
                    f"{json.dumps(enriched_context, ensure_ascii=False, indent=2)}"
                ),
            },
        ],
        temperature=compare_app.QWEN_CONFIG["temperature"],
        max_tokens=max(compare_app.QWEN_CONFIG["max_tokens"], 1800),
    )
    summary = completion.choices[0].message.content
    task_store.store_llm_result(
        cache_key=cache_key,
        task_type="compare_summary",
        model=compare_app.QWEN_CONFIG["model"],
        prompt_version=SUMMARY_PROMPT_VERSION,
        input_hash=input_hash,
        result_text=summary,
    )
    return {"summary": summary, "cached": False, "cache_key": cache_key}


def _parse_uploaded_image_task(task_id: str, image_id: str) -> None:
    try:
        task_store.set_task_state(task_id, status="running", progress=20)
        task_store.set_image_parse_result(image_id, status="running")
        time.sleep(1.5)
        image = task_store.get_image(image_id) or {}
        result = {
            "message": "图片已上传并进入解析队列。当前版本已完成文件落库，OCR 解析可接入后续 worker。",
            "product_name": image.get("product_name"),
            "file_name": image.get("original_filename"),
            "sha256": image.get("sha256"),
            "storage_path": image.get("storage_path"),
        }
        task_store.set_image_parse_result(image_id, status="success", result=result)
        task_store.set_task_state(task_id, status="success", progress=100, result={"image_parse": result})
    except Exception as exc:
        task_store.set_image_parse_result(image_id, status="failed", result={"error": str(exc)})
        task_store.set_task_state(task_id, status="failed", progress=100, error_message=str(exc))


def _run_compare_task(task_id: str, payload: dict) -> None:
    try:
        task_store.set_task_state(task_id, status="running", progress=25)
        result = _build_cat_food_compare_response(payload)
        task_store.set_task_state(task_id, status="success", progress=100, result={"compare": result})
    except Exception as exc:
        task_store.set_task_state(task_id, status="failed", progress=100, error_message=str(exc))


def create_app() -> Flask:
    flask_app = Flask(__name__)
    _start_consumer_apps()
    flask_app.register_blueprint(consumer_api)
    flask_app.register_blueprint(exception_api)
    flask_app.register_blueprint(orchestrator_api)
    flask_app.register_blueprint(pipeline_api)
    flask_app.register_blueprint(product_identity_api)
    flask_app.register_blueprint(process_signal_api)

    @flask_app.get("/health")
    def health() -> tuple[dict, int]:
        return {"status": "ok"}, 200

    @flask_app.get("/")
    def workbench_index():
        return send_from_directory(WEB_DIR, "index.html")

    @flask_app.get("/cat-food-compare.html")
    def cat_food_compare_index():
        if (DIST_DIR / "index.html").exists():
            return send_from_directory(DIST_DIR, "index.html")
        return send_from_directory(WEB_DIR, "index.html")

    @flask_app.get("/assets/<path:filename>")
    def vite_assets(filename: str):
        return send_from_directory(DIST_DIR / "assets", filename)

    @flask_app.get("/workbench.html")
    def workbench_html():
        return send_from_directory(WEB_DIR, "index.html")

    @flask_app.get("/official-site.html")
    def official_site_html():
        return send_from_directory(WEB_DIR, "official-site.html")

    @flask_app.get("/consumer-portal.html")
    def consumer_portal_html():
        return send_from_directory(WEB_DIR, "consumer-portal.html")

    @flask_app.get("/pipeline-review.html")
    def pipeline_review_html():
        return send_from_directory(WEB_DIR, "pipeline-review.html")

    @flask_app.get("/formula-clue-analysis.html")
    def formula_clue_analysis_html():
        return send_from_directory(WEB_DIR, "formula-clue-analysis.html")

    @flask_app.get("/api/cat-food-compare/products")
    def cat_food_compare_products():
        try:
            compare_app = _load_compare_app()
            return jsonify({"products": compare_app.load_product_options()})
        except Exception as exc:
            return _json_error(f"产品库加载失败：{exc}", 500)

    @flask_app.get("/api/cat-food-compare/brands")
    def cat_food_compare_brands():
        try:
            return jsonify({"brands": _load_catfood_brand_options()})
        except Exception as exc:
            return _json_error(f"品牌库加载失败：{exc}", 500)

    @flask_app.post("/api/cat-food/tasks")
    def create_cat_food_task():
        payload = request.get_json(silent=True) or {}
        task_type = str(payload.get("task_type") or "cat_food_compare").strip()
        if task_type not in {"cat_food_compare", "image_parse", "compare_summary"}:
            return _json_error("不支持的任务类型。")
        try:
            task = task_store.create_task(task_type, payload)
            return jsonify({"task": task}), 201
        except Exception as exc:
            return _json_error(f"任务创建失败：{exc}", 500)

    @flask_app.get("/api/cat-food/tasks/<task_id>")
    def get_cat_food_task(task_id: str):
        task = task_store.get_task(task_id)
        if not task:
            return _json_error("任务不存在。", 404)
        return jsonify({"task": task})

    @flask_app.post("/api/cat-food/tasks/<task_id>/images")
    def upload_cat_food_task_image(task_id: str):
        task = task_store.get_task(task_id)
        if not task:
            return _json_error("任务不存在。", 404)
        image_file = request.files.get("image")
        if not image_file or not image_file.filename:
            return _json_error("请上传图片文件。")
        content_type = image_file.content_type or ""
        if content_type and not content_type.startswith("image/"):
            return _json_error("请上传 jpg、png、webp 等图片格式。")

        brand_name = str(request.form.get("brand_name") or "").strip()
        product_name = str(request.form.get("product_name") or "").strip()
        filename = _build_uploaded_image_filename(product_name, image_file.filename)
        image_dir = task_store.UPLOAD_DIR / task_id
        image_dir.mkdir(parents=True, exist_ok=True)
        storage_path = _dedupe_storage_path(image_dir / filename)
        image_file.save(storage_path)
        image = task_store.add_uploaded_image(
            task_id,
            product_name=product_name,
            original_filename=image_file.filename,
            storage_path=storage_path,
            content_type=content_type,
            file_size=storage_path.stat().st_size,
        )
        orchestrator_task = create_orchestrator_task(
            "catfood_image_analysis",
            {
                "cat_food_task_id": task_id,
                "image_id": image["id"],
                "brand_name": brand_name,
                "product_name": image.get("product_name"),
                "image_path": image.get("storage_path"),
                "original_filename": image.get("original_filename"),
                "content_type": image.get("content_type"),
                "file_size": image.get("file_size"),
                "sha256": image.get("sha256"),
            },
        )
        orchestrator_task = apply_orchestrator_node_result(
            orchestrator_task["id"],
            "upload_check",
            call_status="success",
            output={
                "image_path": image.get("storage_path"),
                "image_id": image["id"],
                "brand_name": brand_name,
                "product_name": image.get("product_name"),
                "original_filename": image.get("original_filename"),
                "content_type": image.get("content_type"),
                "file_size": image.get("file_size"),
                "sha256": image.get("sha256"),
            },
        )
        _cat_food_task_executor.submit(_parse_uploaded_image_task, task_id, image["id"])
        return jsonify({
            "task": task_store.get_task(task_id),
            "image": image,
            "orchestrator_task": orchestrator_task,
        }), 202

    @flask_app.post("/api/cat-food/tasks/<task_id>/compare")
    def start_cat_food_compare_task(task_id: str):
        if not task_store.get_task(task_id):
            return _json_error("任务不存在。", 404)
        payload = request.get_json(silent=True) or {}
        try:
            task_store.update_profile(task_id, payload)
            task_store.set_task_state(task_id, status="pending", progress=0, result=None, error_message=None)
            _cat_food_task_executor.submit(_run_compare_task, task_id, payload)
            return jsonify({"task": task_store.get_task(task_id)}), 202
        except Exception as exc:
            return _json_error(f"对比任务启动失败：{exc}", 500)

    @flask_app.post("/api/cat-food/tasks/<task_id>/summary")
    def cat_food_compare_task_summary(task_id: str):
        task = task_store.get_task(task_id)
        if not task:
            return _json_error("任务不存在。", 404)
        payload = request.get_json(silent=True) or {}
        try:
            summary_result = _generate_cat_food_summary(payload)
            result = dict(task.get("result") or {})
            result["summary"] = summary_result
            task_store.set_task_state(task_id, status="success", progress=100, result=result)
            return jsonify(summary_result)
        except ValueError as exc:
            return _json_error(str(exc))
        except Exception as exc:
            return _json_error(f"大模型总结生成失败：{exc}", 500)

    @flask_app.post("/api/cat-food-compare/compare")
    def cat_food_compare_compare():
        payload = request.get_json(silent=True) or {}
        try:
            return jsonify(_build_cat_food_compare_response(payload))
        except ValueError as exc:
            return _json_error(str(exc))
        except Exception as exc:
            return _json_error(f"对比数据生成失败：{exc}", 500)
        current_query = str(payload.get("current_food") or "").strip()
        target_query = str(payload.get("target_food") or "").strip()
        if not current_query or not target_query:
            return _json_error("请选择当前粮和对比粮。")
        if current_query == target_query:
            return _json_error("当前粮和对比粮不能相同。")

        try:
            try:
                from .consumer_apps import product_compare_qwen as compare_app
            except ImportError:
                from consumer_apps import product_compare_qwen as compare_app

            current_product = compare_app.build_product_context(current_query)
            target_product = compare_app.build_product_context(target_query)
            diff_df = compare_app.build_profile_diff(current_product, target_product)
            core_diff_explanations = compare_app.build_core_diff_explanations(
                diff_df,
                current_product["name"],
                target_product["name"],
            )
            tag_diff_summary = compare_app.build_tag_diff_summary(current_product, target_product)
            need_focus_df = compare_app.build_need_focus_table(
                diff_df,
                current_product["name"],
                target_product["name"],
            )
            llm_context = compare_app.build_llm_compare_context(
                product_a=current_product,
                product_b=target_product,
                diff_df=diff_df,
                core_diff_explanations=core_diff_explanations,
                tag_diff_summary=tag_diff_summary,
                need_focus_df=need_focus_df,
            )
            response = {
                "current_food": {
                    "query": current_query,
                    "name": current_product["name"],
                    "brand_name": current_product["brand_name"],
                    "ingredient_composition": current_product.get("ingredient_composition") or "",
                    "profile": _profile_records(current_product),
                    "baseline_profile": _baseline_records(current_product),
                },
                "target_food": {
                    "query": target_query,
                    "name": target_product["name"],
                    "brand_name": target_product["brand_name"],
                    "ingredient_composition": target_product.get("ingredient_composition") or "",
                    "profile": _profile_records(target_product),
                    "baseline_profile": _baseline_records(target_product),
                },
                "profile_diff": compare_app.make_json_safe(diff_df.to_dict(orient="records")),
                "friendly_rows": _build_friendly_rows(current_product, target_product),
                "core_diff_explanations": compare_app.make_json_safe(core_diff_explanations),
                "tag_diff_summary": compare_app.make_json_safe(tag_diff_summary),
                "llm_context": compare_app.make_json_safe(llm_context),
            }
            return jsonify(response)
        except Exception as exc:
            return _json_error(f"对比数据生成失败：{exc}", 500)

    @flask_app.post("/api/cat-food-compare/summary")
    def cat_food_compare_summary():
        payload = request.get_json(silent=True) or {}
        try:
            return jsonify(_generate_cat_food_summary(payload))
        except ValueError as exc:
            return _json_error(str(exc))
        except Exception as exc:
            return _json_error(f"大模型总结生成失败：{exc}", 500)
        context = payload.get("llm_context")
        friendly_rows = payload.get("friendly_rows") or []
        cat_profile = payload.get("cat_profile") or {}
        if not context:
            return _json_error("缺少大模型上下文。")

        try:
            try:
                from .consumer_apps import product_compare_qwen as compare_app
            except ImportError:
                from consumer_apps import product_compare_qwen as compare_app

            enriched_context = {
                "comparison_mode": "当前粮到对比粮的换粮判断",
                "cat_profile": cat_profile,
                "friendly_rows": friendly_rows,
                "base_context": context,
            }
            product_a = context.get("product_a", {}) if isinstance(context, dict) else {}
            product_b = context.get("product_b", {}) if isinstance(context, dict) else {}
            product_a_full_name = " ".join(
                part for part in [str(product_a.get("brand_name") or "").strip(), str(product_a.get("name") or "").strip()] if part
            ) or "产品A"
            product_b_full_name = " ".join(
                part for part in [str(product_b.get("brand_name") or "").strip(), str(product_b.get("name") or "").strip()] if part
            ) or "产品B"
            system_prompt = """
你是一个面向猫主人用户的猫粮对比解读助手。

你的任务不是写专业分析报告，而是把两款猫粮的模型评分、风险标签和适用场景，转化成普通宠主能快速看懂的“怎么选”建议。

请严格按照以下格式输出，不要增加额外章节：

【一句话总结】
用1句话告诉用户：哪款适合什么目标，健康成年猫默认更推荐哪款。
语言要直接、口语化，可以使用“追求……选【完整产品名】，追求……选【完整产品名】”这种句式。

【🐱 产品A完整品牌+产品名称】
用4条 bullet 输出：
* ✅ 主要优势1：必须结合关键分数或标签
* ✅/⚠️ 主要优势或风险2：必须解释成用户能理解的喂养影响
* ⚠️ 主要风险：说明适合避开什么猫
* 🎯 适合：用一句话说明适合哪类猫

【🐱 产品B完整品牌+产品名称】
用4条 bullet 输出：
* ✅ 主要优势1：必须结合关键分数或标签
* ✅/⚠️ 主要优势或风险2：必须解释成用户能理解的喂养影响
* ⚠️ 主要短板：不要夸大，和产品A完整品牌+产品名称做相对比较
* 🎯 适合：用一句话说明适合哪类猫

【🔄 从产品A完整品牌+产品名称换到产品B完整品牌+产品名称，可能改善】
只输出3条 bullet。
必须是宠主能观察到的变化，例如：下巴出油、发黑、软便、便便成型、呕吐、食欲、消化负担。
不要写抽象指标名称。

【⚠️ 注意】
输出1句话。
必须包含：换粮需要7天左右过渡；如果症状严重或长期存在，应咨询兽医。
不要做医疗诊断。

【抄作业：到底怎么选？】
先写一句：“请根据猫咪实际身体状况对号入座：”
然后输出2条：
* 👉 选【产品A完整品牌+产品名称】：列出2-4种适合情况，语言口语化，但不要绝对化
* 👉 选【产品B完整品牌+产品名称】：列出2-4种适合情况，语言口语化，但不要绝对化

写作要求：
1. 面向C端宠主，语言要像小红书/导购建议，但不要夸张营销。
2. 不要使用“分位值、产品池、模型判断、CTQ、配方画像”等专业词，除非已经翻译成用户能懂的话。
3. 每个分数最多保留1位小数。
4. 数值只保留最关键的3-5个，不要堆满所有指标。
5. 不要说“一定改善”“根治”“治疗”等绝对表达。
6. 如果两款粮都有共同风险，要在注意事项里提醒。
7. 如果猫咪是健康成年猫且无历史问题，默认推荐更稳、更低风险、更适合长期喂养的一款。
8. 输出要短，整体控制在500字以内。
9. 不要在【抄作业：到底怎么选？】两条 bullet 的固定句式里使用“如果”两个字。
10. 所有出现产品名的位置，都必须使用完整的“品牌+产品名称”，不要写“产品A”“产品B”“当前粮”“对比粮”。
"""
            client = compare_app.get_qwen_client()
            completion = client.chat.completions.create(
                model=compare_app.QWEN_CONFIG["model"],
                messages=[
                    {
                        "role": "system",
                        "content": system_prompt.strip(),
                    },
                    {
                        "role": "user",
                        "content": (
                            "请只基于下面结构化数据输出，不要编造成分、疾病诊断或输入中不存在的结论。"
                            f"产品A完整名称是：{product_a_full_name}。产品B完整名称是：{product_b_full_name}。"
                            "请严格按 system 中指定格式输出，并把模板中的产品A完整品牌+产品名称、产品B完整品牌+产品名称替换成上述完整名称。\n\n"
                            f"{json.dumps(enriched_context, ensure_ascii=False, indent=2)}"
                        ),
                    },
                ],
                temperature=compare_app.QWEN_CONFIG["temperature"],
                max_tokens=max(compare_app.QWEN_CONFIG["max_tokens"], 1800),
            )
            return jsonify({"summary": completion.choices[0].message.content})
        except Exception as exc:
            return _json_error(f"大模型总结生成失败：{exc}", 500)

    @flask_app.get("/consumer/recommendation-engine")
    def consumer_recommendation_engine():
        return redirect(_consumer_app_url("consumer/recommendation-engine", 8501), code=302)

    @flask_app.get("/consumer/product-display")
    def consumer_product_display():
        return redirect("/consumer/recommendation-engine", code=302)

    @flask_app.get("/consumer/product-compare")
    def consumer_product_compare():
        return redirect(_consumer_app_url("consumer/product-compare", 8502), code=302)

    return flask_app


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
