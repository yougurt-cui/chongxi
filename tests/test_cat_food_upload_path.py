from services import orchestrator_service


def test_upload_check_accepts_date_directory_under_upload_root(tmp_path, monkeypatch):
    upload_root = tmp_path / "cat_food_uploads"
    image_path = upload_root / "20260804" / "K36_0804123000.png"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"image")
    monkeypatch.setattr(orchestrator_service, "FIXED_UPLOAD_ROOT", upload_root)

    result = orchestrator_service._check_upload({"image_path": str(image_path)}, {})

    assert result.passed is True
    assert result.status == orchestrator_service.NODE_SUCCESS
