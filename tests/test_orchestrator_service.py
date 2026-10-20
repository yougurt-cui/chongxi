from services import orchestrator_service as service


def test_image_upload_forces_reparse_of_current_source():
    task = {
        "id": "task-1",
        "task_type": "catfood_image_analysis",
        "payload": {
            "image_path": "/tmp/example.jpg",
            "sha256": "abc123",
        },
        "outputs": {},
    }
    node = next(
        item for item in service.get_pipeline_definition("catfood_image_analysis")
        if item.node_code == "ocr_formula"
    )

    payload = service.build_node_input(task, node)

    assert payload["incremental_only"] is True
    assert payload["reparse_current_upload"] is True
    assert payload["sha256"] == "abc123"


def test_ocr_formula_accepts_ingredient_list_without_heading():
    result = service._check_ocr_formula(
        {
            "ocr_text": "带骨鲜鸡肉45%、鲜鸡胸肉22%、鸡肉粉11%、红薯颗粒、鸡油4%",
            "nutrition_values": {"粗蛋白": "≥45%", "粗脂肪": "≥18%"},
        }
    )

    assert result.passed is True
    assert result.status == service.NODE_SUCCESS


def test_ocr_formula_accepts_columnar_nutrition_table():
    ocr_text = """产品原料
原料组成：新鲜三文鱼13.59%、三文鱼粉13.22%、豌豆、牛油
产品成分分析保证值
项目
粗蛋白质
粗脂肪
粗纤维
粗灰分
钙
总磷
水分
牛磺酸
分析保证值
≥37.0%
≥16.0%
≤6.0%
≤10.0%
≥0.5%
≥0.5%
≤10.0%
≥0.1%
每日推荐饲喂指南"""

    result = service._check_ocr_formula({"ocr_text": ocr_text})

    assert result.passed is True
    assert result.status == service.NODE_SUCCESS
    assert result.details["nutrition_pattern_count"] == 4


def test_manual_ocr_edit_preserves_original_structured_output(monkeypatch):
    original_output = {
        "source_id": 462,
        "ocr_text": "旧文本",
        "nutrition_values": {"粗蛋白": "≥45%", "粗脂肪": "≥18%"},
    }
    task = {
        "id": "task-1",
        "outputs": {"ocr_formula": original_output},
    }
    captured = {}

    monkeypatch.setattr(service, "get_task", lambda task_id: task)

    def fake_apply_node_result(task_id, node_code, *, call_status, output, error_message=None):
        captured.update(output)
        return {"id": task_id}

    monkeypatch.setattr(service, "apply_node_result", fake_apply_node_result)

    service.apply_manual_ocr_text("task-1", "配料：鲜鸡肉45%、鸡肉粉11%、鱼油", "reviewer")

    assert captured["source_id"] == 462
    assert captured["nutrition_values"] == original_output["nutrition_values"]
    assert captured["ocr_text"].startswith("配料：")
    assert captured["manual_review"] is True


def test_repair_incomplete_brand_standardize_output(monkeypatch):
    task = {
        "id": "task-1",
        "task_type": "catfood_image_analysis",
        "payload": {},
        "outputs": {
            "ocr_formula": {
                "source_id": 463,
                "parsed_row_id": 577,
                "brand": "安诺",
            }
        },
    }

    def fake_call_node(called_task, node_def):
        assert called_task is task
        assert node_def.node_code == "brand_standardize"
        return {
            "ok": True,
            "source_id": 463,
            "brand_id": 12,
            "brand_status": "matched",
            "standard_brand_name": "安诺",
        }

    monkeypatch.setattr(service, "_call_node", fake_call_node)

    repaired = service._repair_incomplete_standardization_output(
        task,
        "brand_standardize",
        {"source_id": 463, "product_key": ""},
    )

    assert repaired["brand_status"] == "matched"
    assert repaired["brand_id"] == 12
    assert repaired["source_id"] == 463
