"""Process signal API entrypoints."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from app.services.process_signal_service import run_process_signal_pipeline, write_process_signals


process_signal_api = Blueprint("process_signal_api", __name__, url_prefix="/api")


@process_signal_api.post("/process-signals/run")
def process_signals_run():
    payload = request.get_json(silent=True) or {}
    try:
        result = run_process_signal_pipeline(payload)
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@process_signal_api.post("/process-signals")
def process_signals_write():
    payload = request.get_json(silent=True) or {}
    try:
        result = write_process_signals(payload)
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
