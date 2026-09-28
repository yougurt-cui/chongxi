#!/usr/bin/env python3
"""Create one reviewed pet-content draft from an existing material."""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from services.pet_content_operations_service import generate_content_task, list_materials  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="从 pet_material 生成宠物运营内容草稿")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--material-id", type=int)
    group.add_argument("--random", action="store_true", help="从已审核素材中随机选择")
    parser.add_argument("--style", default="")
    parser.add_argument("--operator", default="cli")
    args = parser.parse_args()

    material_id = args.material_id
    if args.random:
        items = list_materials(review_status="approved", page_size=100)["items"]
        if not items:
            raise RuntimeError("没有已审核的可用素材")
        material_id = random.choice(items)["id"]
    result = generate_content_task(material_id, style=args.style, created_by=args.operator)
    item = result["item"]
    print({"id": item["id"], "material_id": item["material_id"], "status": item["status"], "error": item.get("error_message")})


if __name__ == "__main__":
    main()
