"""Pipeline API entrypoints."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from app.services.pipeline_service import ingest_catfood_ingredients


pipeline_api = Blueprint("pipeline_api", __name__, url_prefix="/api")


@pipeline_api.post("/catfood/ingredients/ingest")
def catfood_ingredients_ingest():
    payload = request.get_json(silent=True) or {}
    try:
        result = ingest_catfood_ingredients(payload)
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

