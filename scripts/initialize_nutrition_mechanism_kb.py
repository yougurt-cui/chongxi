#!/usr/bin/env python3
"""Initialize nutrition mechanism knowledge schemas without importing knowledge."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.nutrition_mechanism_kb_service import (  # noqa: E402
    KNOWLEDGE_TABLES,
    USER_ASSESSMENT_TABLE,
    initialize_nutrition_mechanism_tables,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-user-assessment", action="store_true")
    args = parser.parse_args()
    tables = list(KNOWLEDGE_TABLES)
    if not args.skip_user_assessment:
        tables.append(USER_ASSESSMENT_TABLE)
    if args.dry_run:
        result = {
            "ok": True,
            "dry_run": True,
            "tables": tables,
            "excluded_tables": ["catfood_formula_mechanism_profile"],
        }
    else:
        result = initialize_nutrition_mechanism_tables(
            include_user_assessment=not args.skip_user_assessment
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
