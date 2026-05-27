"""Consumer analysis API entrypoints."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from app.services.consumer_analysis_service import (
    calculate_material_scores,
    calculate_risk_scores,
    collect_consumer_comments,
    engineer_consumer_features,
    structure_consumer_disease_clues,
)


consumer_api = Blueprint("consumer_api", __name__, url_prefix="/api/consumer")


@consumer_api.post("/features/engineer")
def consumer_features_engineer():
    payload = request.get_json(silent=True) or {}
    try:
        result = engineer_consumer_features(payload)
        return jsonify(result), 200 if result.get("ok") else 500
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@consumer_api.post("/comments/collect")
def consumer_comments_collect():
    payload = request.get_json(silent=True) or {}
    try:
        result = collect_consumer_comments(payload)
        return jsonify(result), 200 if result.get("ok") else 500
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


def _disease_structure_payload() -> dict:
    if request.method == "GET":
        payload = request.args.to_dict()
        for key in ("limit", "batch_size", "min_id", "max_id", "max_retries", "max_comment_chars"):
            if key in payload and payload[key] != "":
                payload[key] = int(payload[key])
        if "skip_existing" in payload:
            payload["skip_existing"] = payload["skip_existing"].lower() not in {"0", "false", "no"}
        return payload
    return request.get_json(silent=True) or {}


@consumer_api.route("/disease/structure", methods=["GET", "POST"])
def consumer_disease_structure():
    payload = _disease_structure_payload()
    try:
        result = structure_consumer_disease_clues(payload)
        return jsonify(result), 200 if result.get("ok") else 500
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@consumer_api.post("/materials/scores/calculate")
def consumer_material_scores_calculate():
    payload = request.get_json(silent=True) or {}
    try:
        result = calculate_material_scores(payload)
        return jsonify(result), 200 if result.get("ok") else 500
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@consumer_api.post("/risks/calculate")
def consumer_risks_calculate():
    payload = request.get_json(silent=True) or {}
    try:
        result = calculate_risk_scores(payload)
        return jsonify(result), 200 if result.get("ok") else 500
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
