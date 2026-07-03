#!/usr/bin/env python3
"""Import validated incremental standard ingredients and aliases from extracted workbook JSON."""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import pymysql

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app_config import get_mysql_config  # noqa: E402


def _rows(matrix):
    headers = [str(value or "").strip() for value in matrix[0]]
    return [dict(zip(headers, row)) for row in matrix[1:] if any(value not in (None, "") for value in row)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-json", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    payload = json.loads(args.input_json.read_text(encoding="utf-8"))
    standards, aliases = _rows(payload["standards"]), _rows(payload["aliases"])
    conn = pymysql.connect(**get_mysql_config(), cursorclass=pymysql.cursors.DictCursor, autocommit=False)
    with conn, conn.cursor() as cursor:
        cursor.execute("SELECT * FROM catfood_standard_ingredient")
        existing_standards = {row["standard_ingredient_id"]: row for row in cursor.fetchall()}
        cursor.execute("SELECT * FROM catfood_standard_ingredient_alias WHERE active=1")
        existing_aliases = list(cursor.fetchall())
        incoming_ids = {str(row["standard_ingredient_id"]).strip() for row in standards}
        valid_ids = set(existing_standards) | incoming_ids
        id_conflicts, name_conflicts, missing_refs, alias_conflicts = [], [], [], []
        active_name_to_ids = {}
        for sid, row in existing_standards.items():
            if int(row.get("active") or 0):
                active_name_to_ids.setdefault(str(row["standard_name"]).strip().lower(), set()).add(sid)
        alias_to_ids = {}
        for row in existing_aliases:
            alias_to_ids.setdefault(str(row["alias_name"]).strip().lower(), set()).add(row["standard_ingredient_id"])
        for row in standards:
            sid, name = str(row["standard_ingredient_id"]).strip(), str(row["standard_name"]).strip()
            existing = existing_standards.get(sid)
            if existing and str(existing["standard_name"]).strip() != name:
                id_conflicts.append({"id": sid, "incoming": name, "existing": existing["standard_name"]})
            other_ids = active_name_to_ids.get(name.lower(), set()) - {sid}
            if other_ids:
                name_conflicts.append({"name": name, "incoming_id": sid, "existing_ids": sorted(other_ids)})
        for row in aliases:
            sid, name = str(row["standard_ingredient_id"]).strip(), str(row["alias_name"]).strip()
            if sid not in valid_ids:
                missing_refs.append({"standard_ingredient_id": sid, "alias_name": name})
            other_ids = alias_to_ids.get(name.lower(), set()) - {sid}
            if other_ids:
                alias_conflicts.append({"alias_name": name, "incoming_id": sid, "existing_ids": sorted(other_ids)})
        conflicts = id_conflicts + name_conflicts + missing_refs + alias_conflicts
        inserted_standards = inserted_aliases = 0
        backups = []
        if args.apply:
            if conflicts:
                raise ValueError(json.dumps({"conflicts": conflicts}, ensure_ascii=False))
            suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
            for table in ("catfood_standard_ingredient", "catfood_standard_ingredient_alias"):
                backup = f"{table}_increment_{suffix}"
                cursor.execute(f"CREATE TABLE `{backup}` LIKE `{table}`")
                cursor.execute(f"INSERT INTO `{backup}` SELECT * FROM `{table}`")
                backups.append(backup)
            for row in standards:
                cursor.execute(
                    """INSERT INTO catfood_standard_ingredient(
                      standard_ingredient_id,standard_name,ingredient_family,source_type,
                      animal_source,primary_nutrition_role,active
                    ) VALUES(%s,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE standard_name=VALUES(standard_name),
                      ingredient_family=VALUES(ingredient_family),source_type=VALUES(source_type),
                      animal_source=VALUES(animal_source),primary_nutrition_role=VALUES(primary_nutrition_role),
                      active=VALUES(active),updated_at=NOW()""",
                    (str(row["standard_ingredient_id"]).strip(), str(row["standard_name"]).strip(),
                     row.get("ingredient_family"), row.get("source_type"), row.get("animal_source"),
                     row.get("primary_nutrition_role"), int(row.get("active") or 1)),
                )
                inserted_standards += 1
            for row in aliases:
                sid, name = str(row["standard_ingredient_id"]).strip(), str(row["alias_name"]).strip()
                cursor.execute(
                    """INSERT INTO catfood_standard_ingredient_alias(
                      standard_ingredient_id,alias_name,normalized_alias,source,confidence,active
                    ) SELECT %s,%s,%s,%s,%s,%s WHERE NOT EXISTS(
                      SELECT 1 FROM catfood_standard_ingredient_alias
                      WHERE standard_ingredient_id=%s AND alias_name=%s AND active=1)""",
                    (sid, name, str(row.get("normalized_alias") or name).strip(),
                     str(row.get("source") or "incremental_xlsx"), float(row.get("confidence") or 1),
                     int(row.get("active") or 1), sid, name),
                )
                inserted_aliases += int(cursor.rowcount)
            conn.commit()
        else:
            conn.rollback()
        print(json.dumps({"applied": args.apply, "standard_rows": len(standards), "alias_rows": len(aliases),
          "id_conflicts": id_conflicts, "name_conflicts": name_conflicts, "missing_refs": missing_refs,
          "alias_conflicts": alias_conflicts, "inserted_or_updated_standards": inserted_standards,
          "inserted_aliases": inserted_aliases, "backups": backups}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
