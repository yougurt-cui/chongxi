"""Brand/product/formula standardization API."""

from flask import Blueprint, jsonify, request

from services.catfood_standardization_service import (
    build_formula_feature_input,
    get_standard_mapping,
    initialize_brand_candidates,
    init_standardization_db,
    list_brand_candidates,
    list_standard_brands,
    list_standard_products,
    list_standard_ingredients,
    list_ingredient_review_items,
    resolve_ingredient_review,
    review_brand_candidate,
    resolve_brand_mapping,
    resolve_formula_mapping,
    resolve_duplicate_formula_mapping,
    rebuild_formula_feature_inputs,
    standardize_brand,
    standardize_formula,
    standardize_product,
)
from services.formula_feature_link_service import backfill_formula_ids
from services.formula_incremental_service import (
    build_formula_profile,
    materialize_formula_risks,
    materialize_formula_scores,
)
from services.orchestrator_service import resume_brand_tasks_for_source_ids, resume_ingredient_tasks_for_formula_ids


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


@catfood_standardization_api.get("/ingredient-reviews")
def standardization_ingredient_reviews():
    try:
        formula_id = request.args.get("formula_id")
        return jsonify(list_ingredient_review_items(formula_id=int(formula_id) if formula_id else None,
                                                    limit=int(request.args.get("limit") or 200))), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@catfood_standardization_api.get("/ingredients")
def standardization_ingredients():
    try:
        return jsonify({"ok": True, "items": list_standard_ingredients(query=request.args.get("q") or "",
                                                                         limit=int(request.args.get("limit") or 1000))}), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@catfood_standardization_api.get("/products")
def standardization_products():
    try:
        brand_id = request.args.get("brand_id")
        return jsonify({"ok": True, "items": list_standard_products(
            brand_id=int(brand_id) if brand_id else None,
            query=request.args.get("q") or "",
            limit=int(request.args.get("limit") or 1000),
        )}), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@catfood_standardization_api.post("/ingredient-reviews/<int:item_id>/resolve")
def standardization_ingredient_review_resolve(item_id: int):
    try:
        result = resolve_ingredient_review(item_id, request.get_json(silent=True) or {})
        result["resumed_task_ids"] = resume_ingredient_tasks_for_formula_ids([result["formula_id"]])
        return jsonify(result), 200
    except KeyError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@catfood_standardization_api.post("/brand-candidates/<int:candidate_id>/<action>")
def standardization_brand_candidate_review(candidate_id: int, action: str):
    try:
        result = review_brand_candidate(
            candidate_id,
            request.get_json(silent=True) or {},
            action=action,
        )
        result["resumed_task_ids"] = (
            resume_brand_tasks_for_source_ids(result.get("source_ids") or [])
            if result.get("status") == "approved"
            else []
        )
        return jsonify(result), 200
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


@catfood_standardization_api.post("/formula/duplicate/resolve")
def standardization_duplicate_formula_resolve():
    try:
        return jsonify(resolve_duplicate_formula_mapping(request.get_json(silent=True) or {})), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@catfood_standardization_api.post("/formula-input/build")
def standardization_formula_input_build():
    try:
        payload = request.get_json(silent=True) or {}
        return jsonify(
            build_formula_feature_input(
                formula_id=int(payload.get("formula_id")),
                apply=True,
            )
        ), 200
    except KeyError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@catfood_standardization_api.post("/formula-input/rebuild")
def standardization_formula_input_rebuild():
    try:
        return jsonify(rebuild_formula_feature_inputs(apply=True)), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@catfood_standardization_api.post("/formula-profile/build")
def standardization_formula_profile_build():
    try:
        payload = request.get_json(silent=True) or {}
        return jsonify(build_formula_profile(formula_id=int(payload["formula_id"]), apply=True)), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@catfood_standardization_api.post("/formula-materialize")
def standardization_formula_materialize():
    try:
        payload = request.get_json(silent=True) or {}
        result = materialize_formula_scores(formula_id=int(payload["formula_id"]), batch_id=payload.get("batch_id"))
        return jsonify(result), 200 if result.get("ok") else 409
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@catfood_standardization_api.post("/formula-risk/materialize")
def standardization_formula_risk_materialize():
    try:
        payload = request.get_json(silent=True) or {}
        return jsonify(materialize_formula_risks(formula_id=int(payload["formula_id"]), batch_id=payload.get("batch_id"))), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@catfood_standardization_api.post("/formula-links/backfill")
def standardization_formula_links_backfill():
    try:
        return jsonify(backfill_formula_ids()), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
