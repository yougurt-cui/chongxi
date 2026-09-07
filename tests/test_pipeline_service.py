from services import pipeline_service as service


def test_current_upload_is_reparsed_by_resolved_source_id(monkeypatch, tmp_path):
    image_path = tmp_path / "example.jpg"
    image_path.write_bytes(b"image")
    captured = {}

    monkeypatch.setattr(service, "_configured_pipeline_paths", lambda: {})
    monkeypatch.setattr(service, "load_default_db_config", lambda: {})
    monkeypatch.setattr(
        service,
        "import_ingredient_images",
        lambda **kwargs: {"success_samples": [{"file_sha256": "abc123"}]},
    )
    monkeypatch.setattr(
        service,
        "fetch_ocr_context_by_sha256",
        lambda **kwargs: {
            "id": 502,
            "source_id": 502,
            "file_sha256": "abc123",
            "parsed_row_id": 652,
        },
    )

    def fake_parse(**kwargs):
        captured.update(kwargs)
        return {"scanned": 1, "upserted": 1}

    monkeypatch.setattr(service, "parse_ingredient_ocr", fake_parse)

    result = service.ingest_catfood_ingredients({
        "image_path": str(image_path),
        "image_dir": str(tmp_path),
        "image_glob": image_path.name,
        "steps": ["ocr_import", "parse_ocr_json"],
        "incremental_only": True,
        "reparse_current_upload": True,
    })

    assert result["source_id"] == 502
    assert captured["source_id"] == 502
    assert captured["incremental_only"] is True
