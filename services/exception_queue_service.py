"""Service layer for exception recovery queue APIs."""


from typing import Any, Dict

from adapters.exception_queue_adapter import (
    DEFAULT_EXCEPTION_QUEUE_TABLE,
    check_exception_gate,
    list_exceptions,
    record_exception,
    update_exception_status,
)


def recycle_exception(payload: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(payload or {})
    item = record_exception(
        db=payload.get("db"),
        table_name=payload.get("table_name") or DEFAULT_EXCEPTION_QUEUE_TABLE,
        data_scope=payload.get("data_scope") or payload.get("scope"),
        source_table=payload.get("source_table"),
        source_id=payload.get("source_id"),
        business_key=payload.get("business_key"),
        error_code=payload.get("error_code"),
        error_message=payload.get("error_message") or payload.get("message"),
        payload=payload.get("payload"),
        blocked_reason=payload.get("blocked_reason"),
    )
    return {"ok": True, "item": item}


def query_exception_queue(args: Dict[str, Any]) -> Dict[str, Any]:
    args = dict(args or {})
    result = list_exceptions(
        db=args.get("db"),
        table_name=args.get("table_name") or DEFAULT_EXCEPTION_QUEUE_TABLE,
        status=args.get("status"),
        data_scope=args.get("data_scope") or args.get("scope"),
        source_table=args.get("source_table"),
        source_id=args.get("source_id"),
        limit=int(args.get("limit") or 100),
        offset=int(args.get("offset") or 0),
    )
    return {"ok": True, **result}


def change_exception_status(exception_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(payload or {})
    item = update_exception_status(
        db=payload.get("db"),
        table_name=payload.get("table_name") or DEFAULT_EXCEPTION_QUEUE_TABLE,
        exception_id=exception_id,
        status=payload.get("status"),
        reviewer=payload.get("reviewer"),
        review_note=payload.get("review_note"),
        fix_note=payload.get("fix_note"),
    )
    return {"ok": True, "item": item}


def claim_exception(exception_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(payload or {})
    payload["status"] = "in_review"
    return change_exception_status(exception_id, payload)


def mark_exception_fixed(exception_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(payload or {})
    payload["status"] = "fixed"
    return change_exception_status(exception_id, payload)


def release_exception(exception_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(payload or {})
    payload["status"] = "released"
    return change_exception_status(exception_id, payload)


def exception_run_gate(args: Dict[str, Any]) -> Dict[str, Any]:
    args = dict(args or {})
    result = check_exception_gate(
        db=args.get("db"),
        table_name=args.get("table_name") or DEFAULT_EXCEPTION_QUEUE_TABLE,
        data_scope=args.get("data_scope") or args.get("scope"),
        source_table=args.get("source_table"),
        source_id=args.get("source_id"),
        business_key=args.get("business_key"),
    )
    return {"ok": True, **result}
