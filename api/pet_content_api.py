"""Admin APIs for pet material and generated content operations."""

from flask import Blueprint, jsonify, request

from api.enterprise_api import admin_required
from services.pet_content_operations_service import (
    approve_task,
    delete_material,
    generate_content_task,
    get_material_collection_task,
    get_material,
    get_task,
    list_materials,
    list_material_collection_tasks,
    list_tasks,
    publish_task,
    regenerate_task_image,
    start_material_collection,
    update_material,
    update_task_content,
)


pet_content_api = Blueprint("pet_content_api", __name__, url_prefix="/api/admin/pet-content")


def _response(work, success_status=200):
    try:
        return jsonify(work()), success_status
    except LookupError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@pet_content_api.get("/materials")
@admin_required
def materials_list():
    return _response(lambda: list_materials(
        query=request.args.get("q") or "", category=request.args.get("category") or "",
        sub_category=request.args.get("sub_category") or "", review_status=request.args.get("review_status") or "",
        page=request.args.get("page") or 1, page_size=request.args.get("page_size") or 24,
    ))


@pet_content_api.get("/materials/<int:material_id>")
@admin_required
def material_detail(material_id: int):
    return _response(lambda: {"ok": True, "item": get_material(material_id)})


@pet_content_api.patch("/materials/<int:material_id>")
@admin_required
def material_update(material_id: int):
    return _response(lambda: update_material(material_id, request.get_json(silent=True) or {}))


@pet_content_api.delete("/materials/<int:material_id>")
@admin_required
def material_delete(material_id: int):
    return _response(lambda: delete_material(material_id))


@pet_content_api.post("/collection-tasks")
@admin_required
def material_collection_create():
    payload = request.get_json(silent=True) or {}
    return _response(lambda: start_material_collection(
        payload, created_by=payload.get("operator") or "pipeline-review-page",
    ), 202)


@pet_content_api.get("/collection-tasks")
@admin_required
def material_collection_list():
    return _response(lambda: list_material_collection_tasks(request.args.get("limit") or 20))


@pet_content_api.get("/collection-tasks/<task_id>")
@admin_required
def material_collection_detail(task_id: str):
    return _response(lambda: {"ok": True, "item": get_material_collection_task(task_id)})


@pet_content_api.post("/tasks")
@admin_required
def task_create():
    payload = request.get_json(silent=True) or {}
    return _response(lambda: generate_content_task(
        payload.get("material_id"), style=payload.get("style") or "", created_by=payload.get("operator") or "admin",
    ), 202)


@pet_content_api.get("/tasks")
@admin_required
def tasks_list():
    return _response(lambda: list_tasks(status=request.args.get("status") or "", limit=request.args.get("limit") or 100))


@pet_content_api.get("/tasks/<task_id>")
@admin_required
def task_detail(task_id: str):
    return _response(lambda: {"ok": True, "item": get_task(task_id)})


@pet_content_api.patch("/tasks/<task_id>")
@admin_required
def task_update(task_id: str):
    return _response(lambda: update_task_content(task_id, request.get_json(silent=True) or {}))


@pet_content_api.post("/tasks/<task_id>/approve")
@admin_required
def task_approve(task_id: str):
    payload = request.get_json(silent=True) or {}
    return _response(lambda: approve_task(task_id, payload.get("reviewer") or "admin"))


@pet_content_api.post("/tasks/<task_id>/regenerate-image")
@admin_required
def task_regenerate_image(task_id: str):
    return _response(lambda: regenerate_task_image(task_id), 202)


@pet_content_api.post("/tasks/<task_id>/publish")
@admin_required
def task_publish(task_id: str):
    return _response(lambda: publish_task(task_id))
