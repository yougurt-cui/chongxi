"""Exception recovery queue API entrypoints."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from app.services.exception_queue_service import (
    change_exception_status,
    claim_exception,
    exception_run_gate,
    mark_exception_fixed,
    query_exception_queue,
    recycle_exception,
    release_exception,
)


exception_api = Blueprint("exception_api", __name__, url_prefix="/api/exceptions")


@exception_api.post("/recycle")
def exception_recycle():
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(recycle_exception(payload)), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@exception_api.get("")
def exception_list():
    args = request.args.to_dict()
    try:
        return jsonify(query_exception_queue(args)), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@exception_api.get("/gate")
def exception_gate():
    args = request.args.to_dict()
    try:
        return jsonify(exception_run_gate(args)), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@exception_api.patch("/<int:exception_id>")
def exception_update(exception_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(change_exception_status(exception_id, payload)), 200
    except KeyError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@exception_api.post("/<int:exception_id>/claim")
def exception_claim(exception_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(claim_exception(exception_id, payload)), 200
    except KeyError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@exception_api.post("/<int:exception_id>/fixed")
def exception_fixed(exception_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(mark_exception_fixed(exception_id, payload)), 200
    except KeyError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@exception_api.post("/<int:exception_id>/release")
def exception_release(exception_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(release_exception(exception_id, payload)), 200
    except KeyError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
