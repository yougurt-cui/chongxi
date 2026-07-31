"""Business analysis service layer."""

from __future__ import annotations

from typing import Any

from services.product_function_service import get_product_function_positioning
from services.taobao_sku_import_service import list_standardized_product_options


def get_business_summary() -> dict[str, Any]:
    options = list_standardized_product_options(compare_available="true", limit=80)
    items = options.get("items") or []
    brands = {item.get("brand") for item in items if item.get("brand")}
    origins = {item.get("origin_type") for item in items if item.get("origin_type")}
    priced = [item for item in items if item.get("price") is not None]
    return {
        "ok": True,
        "product_count": len(items),
        "brand_count": len(brands),
        "origin_count": len(origins),
        "priced_product_count": len(priced),
        "sample_products": items[:8],
    }


def get_business_product_options(
    *,
    q: str = "",
    brand: str = "",
    origin: str = "",
    price_bucket: str = "",
    compare_available: str | bool | None = "true",
    limit: int = 120,
) -> dict[str, Any]:
    return list_standardized_product_options(
        q=q,
        brand=brand,
        origin=origin,
        price_bucket=price_bucket,
        compare_available=compare_available,
        limit=limit,
    )


def get_business_product_positioning(payload: dict[str, Any]) -> dict[str, Any]:
    result = get_product_function_positioning(
        {
            "source_id": payload.get("source_id") or payload.get("score_source_id"),
            "product_key": payload.get("product_key") or payload.get("sku_id"),
            "brand": payload.get("brand") or payload.get("brand_name"),
            "product_name": payload.get("product_name") or payload.get("sku_name"),
            "include_raw": False,
        }
    )
    if not result.get("ok"):
        return result
    return {
        "ok": True,
        "product": {
            "product_key": payload.get("product_key"),
            "brand": payload.get("brand") or payload.get("brand_name"),
            "product_name": payload.get("product_name") or payload.get("sku_name"),
        },
        "positioning": result.get("item") or {},
        "input_scores": result.get("input_scores") or {},
    }
