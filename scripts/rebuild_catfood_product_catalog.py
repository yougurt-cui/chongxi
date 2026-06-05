#!/usr/bin/env python
"""Rebuild the unified C-side cat-food product catalog."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from services.cat_food_product_catalog_service import (  # noqa: E402
    DEFAULT_BRAND_EXCEL_PATH,
    DEFAULT_TAOBAO_SKU_DIR,
    rebuild_product_catalog,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--brand-excel", default=str(DEFAULT_BRAND_EXCEL_PATH), help="品牌标准化 Excel 路径")
    parser.add_argument("--taobao-dir", default=str(DEFAULT_TAOBAO_SKU_DIR), help="淘宝 SKU JSON 输出目录")
    parser.add_argument("--truncate", action="store_true", help="重建前清空 catalog 表")
    args = parser.parse_args()

    result = rebuild_product_catalog(
        brand_excel_path=args.brand_excel,
        taobao_dir=args.taobao_dir,
        truncate=args.truncate,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
