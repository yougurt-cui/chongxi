#!/usr/bin/env python3
"""Collect public pet-product image search results into csv_labeling.pet_material."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlparse

import pymysql
import requests
from playwright.sync_api import sync_playwright


BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app_config import get_mysql_config, get_qwen_config  # noqa: E402


TABLE_NAME = "pet_material"
VISION_PROMPT = """
你是宠物素材视觉分析器。只分析图片里的视觉元素，不要生图或输出生图提示词。
只返回 JSON：
{
  "pet_type": "cat/dog/both/unknown", "product_type": "产品类型",
  "main_subject": "画面主体", "product_shape": "产品形状", "main_colors": "主要颜色",
  "material_texture": "可见材质和质感", "pet_action": "宠物动作",
  "interaction_type": "宠物与商品如何互动", "usage_scene": "使用场景",
  "composition": "构图", "camera_angle": "镜头角度", "visual_style": "视觉风格",
  "emotion": "画面情绪", "standout_element": "最突出的视觉元素",
  "visual_tags": ["标签1", "标签2", "标签3"]
}
""".strip()

PLATFORM_DOMAINS = {
    "amazon": "amazon.com",
    "youtube": "youtube.com",
    "facebook": "facebook.com",
}

CATEGORIES = [
    {"category": "玩具", "sub_category": "自动互动玩具", "keywords": ["automatic interactive cat toy", "smart cat toy"]},
    {"category": "玩具", "sub_category": "猫薄荷毛绒玩具", "keywords": ["catnip plush toy", "cat kicker toy"]},
    {"category": "玩具", "sub_category": "益智玩具", "keywords": ["pet puzzle toy", "cat puzzle toy"]},
    {"category": "玩具", "sub_category": "逗猫棒", "keywords": ["cat teaser wand", "interactive feather wand cat toy"]},
    {"category": "玩具", "sub_category": "轨道球", "keywords": ["cat track ball toy", "interactive ball track cat toy"]},
    {"category": "玩具", "sub_category": "磨牙玩具", "keywords": ["pet chew toy", "cat dental chew toy"]},
    {"category": "玩具", "sub_category": "漏食玩具", "keywords": ["pet treat dispenser toy", "cat food puzzle feeder toy"]},
    {"category": "玩具", "sub_category": "猫抓板", "keywords": ["cat scratching board", "cardboard cat scratcher"]},
    {"category": "玩具", "sub_category": "猫隧道", "keywords": ["cat tunnel toy", "collapsible cat play tunnel"]},
    {"category": "玩具", "sub_category": "激光玩具", "keywords": ["cat laser toy", "automatic laser toy for cats"]},
    {"category": "衣服", "sub_category": "日常宠物服", "keywords": ["dog hoodie", "cat sweater"]},
    {"category": "衣服", "sub_category": "搞笑Cosplay", "keywords": ["funny pet costume", "cat cosplay costume"]},
]


def get_conn(*, autocommit: bool = False):
    return pymysql.connect(
        **get_mysql_config(database="csv_labeling"),
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=autocommit,
    )


def init_db() -> None:
    with get_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS `{TABLE_NAME}` (
                    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    platform VARCHAR(32) NOT NULL,
                    external_id CHAR(40) NOT NULL,
                    category VARCHAR(64) NOT NULL,
                    sub_category VARCHAR(128) NOT NULL,
                    search_keyword VARCHAR(255) NOT NULL,
                    title VARCHAR(1000) NULL,
                    source_url TEXT NOT NULL,
                    image_url TEXT NULL,
                    search_rank INT NULL,
                    hot_score DECIMAL(10,2) NULL,
                    pet_type VARCHAR(64) NULL,
                    product_type VARCHAR(255) NULL,
                    main_subject VARCHAR(500) NULL,
                    product_shape VARCHAR(500) NULL,
                    main_colors VARCHAR(500) NULL,
                    material_texture VARCHAR(500) NULL,
                    pet_action VARCHAR(500) NULL,
                    interaction_type VARCHAR(500) NULL,
                    usage_scene VARCHAR(500) NULL,
                    composition VARCHAR(500) NULL,
                    camera_angle VARCHAR(255) NULL,
                    visual_style VARCHAR(500) NULL,
                    emotion VARCHAR(500) NULL,
                    standout_element TEXT NULL,
                    visual_tags JSON NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (id),
                    UNIQUE KEY uk_pet_material_platform_external (platform, external_id),
                    KEY idx_pet_material_category (category, sub_category),
                    KEY idx_pet_material_keyword (search_keyword)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
        conn.commit()


def _feature_text(value: Any, max_length: int = 500) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, (dict, list, tuple)):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value).strip()
    return text[:max_length] or None


def save_item(item: dict[str, Any], feature: dict[str, Any] | None = None) -> None:
    feature = feature or {}
    fields = (
        "pet_type", "product_type", "main_subject", "product_shape", "main_colors",
        "material_texture", "pet_action", "interaction_type", "usage_scene", "composition",
        "camera_angle", "visual_style", "emotion", "standout_element",
    )
    with get_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO `{TABLE_NAME}` (
                    platform,external_id,category,sub_category,search_keyword,title,source_url,image_url,
                    search_rank,hot_score,{','.join(fields)},visual_tags
                ) VALUES ({','.join(['%s'] * 25)})
                ON DUPLICATE KEY UPDATE
                    category=VALUES(category),sub_category=VALUES(sub_category),
                    search_keyword=VALUES(search_keyword),title=VALUES(title),
                    source_url=VALUES(source_url),image_url=VALUES(image_url),
                    search_rank=VALUES(search_rank),hot_score=VALUES(hot_score),
                    {','.join(f'{field}=VALUES({field})' for field in fields)},
                    visual_tags=VALUES(visual_tags)
                """,
                (
                    item["platform"], item["external_id"], item["category"], item["sub_category"],
                    item["search_keyword"], item.get("title"), item["source_url"], item.get("image_url"),
                    item.get("search_rank"), item.get("hot_score"),
                    *(
                        _feature_text(feature.get(field), 255 if field in {"pet_type", "product_type", "camera_angle"} else 500)
                        for field in fields
                    ),
                    json.dumps(feature.get("visual_tags", []), ensure_ascii=False),
                ),
            )
        conn.commit()


def make_id(platform: str, source_url: str) -> str:
    return hashlib.sha1(f"{platform}|{source_url}".encode("utf-8")).hexdigest()


def domain_match(url: str, platform: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
        domain = PLATFORM_DOMAINS[platform]
        return host == domain or host.endswith(f".{domain}")
    except (TypeError, ValueError):
        return False


def search_materials(page, platform: str, keyword: str, category: str, sub_category: str, limit: int):
    query = quote_plus(f'site:{PLATFORM_DOMAINS[platform]} "{keyword}"')
    page.goto(f"https://www.bing.com/images/search?q={query}", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1800)
    for _ in range(2):
        page.mouse.wheel(0, 1200)
        page.wait_for_timeout(500)

    results = []
    elements = page.locator("a.iusc")
    for index in range(min(elements.count(), max(limit * 6, 20))):
        if len(results) >= limit:
            break
        try:
            raw = elements.nth(index).get_attribute("m")
            metadata = json.loads(raw) if raw else {}
            image_url = metadata.get("murl") or metadata.get("turl")
            source_url = metadata.get("purl") or metadata.get("surl")
            if not image_url or not source_url or not domain_match(source_url, platform):
                continue
            rank = len(results) + 1
            results.append({
                "platform": platform,
                "external_id": make_id(platform, source_url),
                "category": category,
                "sub_category": sub_category,
                "search_keyword": keyword,
                "title": metadata.get("t") or metadata.get("desc") or "",
                "source_url": source_url,
                "image_url": image_url,
                "search_rank": rank,
                "hot_score": max(0, 100 - (rank - 1) * 5),
            })
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return results


def analyze_visual(image_url: str, title: str, category: str, sub_category: str) -> dict[str, Any]:
    qwen = get_qwen_config({"model": os.getenv("PET_MATERIAL_VISION_MODEL") or "qwen-vl-max"})
    if not qwen["api_key"]:
        raise RuntimeError("未配置 DASHSCOPE_API_KEY/QWEN_API_KEY")
    response = requests.post(
        f"{qwen['base_url']}/chat/completions",
        headers={"Authorization": f"Bearer {qwen['api_key']}", "Content-Type": "application/json"},
        json={
            "model": qwen["model"],
            "messages": [
                {"role": "system", "content": VISION_PROMPT},
                {"role": "user", "content": [
                    {"type": "text", "text": f"标题：{title}\n分类：{category}/{sub_category}\n请以图片本身为主要依据分析。"},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ]},
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        },
        timeout=90,
    )
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"]
    if isinstance(content, dict):
        return content
    content = re.sub(r"^```json\s*|\s*```$", "", str(content).strip())
    result = json.loads(content)
    if not isinstance(result, dict):
        raise ValueError("视觉模型未返回 JSON 对象")
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="采集宠物商品视觉素材")
    parser.add_argument("--platforms", default="amazon,youtube,facebook", help="逗号分隔的平台")
    parser.add_argument("--limit", type=int, default=5, help="每个关键词、每个平台的条数")
    parser.add_argument("--category", help="只运行指定大类，如：玩具")
    parser.add_argument("--sub-category", help="只运行指定子类，如：自动互动玩具")
    parser.add_argument("--keyword", help="只运行指定搜索词")
    parser.add_argument("--headed", action="store_true", help="显示浏览器")
    parser.add_argument("--no-vision", action="store_true", help="跳过视觉模型分析")
    parser.add_argument("--init-only", action="store_true", help="只建表并验证数据库连接")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    platforms = [item.strip().lower() for item in args.platforms.split(",") if item.strip()]
    unknown = sorted(set(platforms) - set(PLATFORM_DOMAINS))
    if unknown:
        raise ValueError(f"不支持的平台: {', '.join(unknown)}")
    if args.limit < 1:
        raise ValueError("--limit 必须大于 0")

    init_db()
    print(f"数据库已就绪: csv_labeling.{TABLE_NAME}")
    if args.init_only:
        return

    total = 0
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not args.headed)
        context = browser.new_context(viewport={"width": 1440, "height": 1200}, locale="en-US")
        page = context.new_page()
        try:
            for config in CATEGORIES:
                if args.category and config["category"] != args.category:
                    continue
                if args.sub_category and config["sub_category"] != args.sub_category:
                    continue
                keywords = [args.keyword] if args.keyword else config["keywords"]
                for keyword in keywords:
                    for platform in platforms:
                        print(f"搜索 {platform}: {config['category']}/{config['sub_category']} - {keyword}")
                        try:
                            items = search_materials(
                                page, platform, keyword, config["category"], config["sub_category"], args.limit
                            )
                        except Exception as exc:
                            print(f"  搜索失败: {exc}")
                            continue
                        for item in items:
                            feature: dict[str, Any] = {}
                            if not args.no_vision and item.get("image_url"):
                                try:
                                    feature = analyze_visual(
                                        item["image_url"], item.get("title") or "",
                                        config["category"], config["sub_category"],
                                    )
                                except Exception as exc:
                                    print(f"  视觉分析失败，仍保存原始素材: {exc}")
                            save_item(item, feature)
                            total += 1
                            print(f"  已保存: {item.get('title', '')[:60]}")
        finally:
            context.close()
            browser.close()
    print(f"完成，共处理 {total} 条素材；csv_labeling.{TABLE_NAME}")


if __name__ == "__main__":
    main()
