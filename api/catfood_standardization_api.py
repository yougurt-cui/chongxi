"""Brand/product/formula standardization API."""

from flask import Blueprint, jsonify, request

from services.catfood_standardization_service import (
    get_standard_mapping,
    initialize_brand_candidates,
    init_standardization_db,
    list_brand_candidates,
    list_standard_brands,
    review_brand_candidate,
    resolve_brand_mapping,
    resolve_formula_mapping,
    standardize_brand,
    standardize_formula,
    standardize_product,
)


catfood_standardization_api = Blueprint(
    "catfood_standardization_api",
    __name__,
    url_prefix="/api/catfood/standardization",
)


@catfood_standardization_api.post("/init")
def standardization_init():
    try:
        init_standardization_db()
        return jsonify({"ok": True}), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@catfood_standardization_api.post("/brand")
def standardization_brand():
    try:
        return jsonify(standardize_brand(request.get_json(silent=True) or {})), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@catfood_standardization_api.post("/product")
def standardization_product():
    try:
        return jsonify(standardize_product(request.get_json(silent=True) or {})), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@catfood_standardization_api.post("/brand/resolve")
def standardization_brand_resolve():
    try:
        return jsonify(resolve_brand_mapping(request.get_json(silent=True) or {})), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@catfood_standardization_api.post("/formula")
def standardization_formula():
    try:
        return jsonify(standardize_formula(request.get_json(silent=True) or {})), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@catfood_standardization_api.get("/mappings/<int:source_id>")
def standardization_mapping(source_id: int):
    try:
        item = get_standard_mapping(source_id)
        if not item:
            return jsonify({"ok": False, "error": "mapping not found"}), 404
        return jsonify({"ok": True, "item": item}), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@catfood_standardization_api.get("/brands")
def standardization_brands():
    try:
        items = list_standard_brands(
            query=str(request.args.get("q") or ""),
            limit=int(request.args.get("limit") or 300),
        )
        return jsonify({"ok": True, "items": items}), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@catfood_standardization_api.post("/brand-candidates/init")
def standardization_brand_candidates_init():
    try:
        return jsonify({"ok": True, **initialize_brand_candidates()}), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@catfood_standardization_api.get("/brand-candidates")
def standardization_brand_candidates():
    try:
        return jsonify(
            list_brand_candidates(
                status=str(request.args.get("status") or "pending"),
                limit=int(request.args.get("limit") or 200),
            )
        ), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@catfood_standardization_api.post("/brand-candidates/<int:candidate_id>/<action>")
def standardization_brand_candidate_review(candidate_id: int, action: str):
    try:
        return jsonify(
            review_brand_candidate(
                candidate_id,
                request.get_json(silent=True) or {},
                action=action,
            )
        ), 200
    except KeyError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@catfood_standardization_api.post("/formula/resolve")
def standardization_formula_resolve():
    try:
        return jsonify(resolve_formula_mapping(request.get_json(silent=True) or {})), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
