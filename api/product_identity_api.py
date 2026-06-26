"""Product identity correction API."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from services.product_identity_service import (
    apply_identity_correction,
    init_identity_db,
    list_identity_corrections,
    list_identity_review_items,
    list_product_candidate_reviews,
    lookup_identity,
    review_product_candidate,
)


product_identity_api = Blueprint("product_identity_api", __name__, url_prefix="/api/product-identity")


@product_identity_api.get("/lookup")
def product_identity_lookup():
    try:
        source_id = request.args.get("source_id")
        parsed_row_id = request.args.get("parsed_row_id")
        item = lookup_identity(
            source_id=int(source_id) if source_id else None,
            parsed_row_id=int(parsed_row_id) if parsed_row_id else None,
            file_sha256=request.args.get("file_sha256"),
            image_name=request.args.get("image_name"),
        )
        if not item:
            return jsonify({"ok": False, "error": "未找到身份记录"}), 404
        return jsonify({"ok": True, "item": item}), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@product_identity_api.get("/corrections")
def product_identity_list_corrections():
    try:
        source_id = request.args.get("source_id")
        return jsonify(
            list_identity_corrections(
                source_id=int(source_id) if source_id else None,
                limit=int(request.args.get("limit") or 50),
            )
        ), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@product_identity_api.get("/reviews")
def product_identity_reviews():
    try:
        return jsonify(list_identity_review_items(limit=int(request.args.get("limit") or 100))), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@product_identity_api.post("/corrections")
def product_identity_create_correction():
    payload = request.get_json(silent=True) or {}
    try:
        result = apply_identity_correction(payload)
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@product_identity_api.get("/product-reviews")
def product_candidate_reviews():
    try:
        product_id = request.args.get("product_id")
        return jsonify(
            list_product_candidate_reviews(
                status=str(request.args.get("status") or ""),
                quality=str(request.args.get("quality") or ""),
                brand=str(request.args.get("brand") or ""),
                product_id=int(product_id) if product_id else None,
                limit=int(request.args.get("limit") or 100),
                offset=int(request.args.get("offset") or 0),
            )
        ), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@product_identity_api.post("/product-reviews/<int:product_id>/approve")
def product_candidate_approve(product_id: int):
    try:
        return jsonify(
            review_product_candidate(
                product_id,
                request.get_json(silent=True) or {},
                action="approve",
            )
        ), 200
    except KeyError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@product_identity_api.post("/product-reviews/<int:product_id>/reject")
def product_candidate_reject(product_id: int):
    try:
        return jsonify(
            review_product_candidate(
                product_id,
                request.get_json(silent=True) or {},
                action="reject",
            )
        ), 200
    except KeyError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@product_identity_api.post("/init")
def product_identity_init():
    try:
        init_identity_db()
        return jsonify({"ok": True}), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
