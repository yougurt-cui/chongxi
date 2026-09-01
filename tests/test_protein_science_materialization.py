from scripts.rebuild_protein_source_from_profiles import _is_missing_protein_science


def test_legacy_protein_flag_does_not_override_active_carbohydrate_science():
    assert not _is_missing_protein_science({
        "is_protein": 1,
        "primary_nutrition_role": "碳水供给",
        "science_status": "active",
        "science_nutrition_category": "carbohydrate",
    })


def test_non_protein_primary_roles_do_not_require_protein_science():
    assert not _is_missing_protein_science({
        "is_protein": 1,
        "primary_nutrition_role": "适口性支持",
        "science_status": "draft",
        "science_nutrition_category": "other",
    })


def test_protein_primary_role_requires_active_protein_science():
    assert _is_missing_protein_science({
        "primary_nutrition_role": "蛋白质供给",
        "science_status": "draft",
        "science_nutrition_category": "protein",
    })
    assert not _is_missing_protein_science({
        "primary_nutrition_role": "蛋白质供给",
        "science_status": "active",
        "science_nutrition_category": "protein",
    })
