#!/usr/bin/env python3
"""Rebuild ingredient aliases and active standards from the reviewed workbook JSON."""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pymysql

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))
from app_config import get_mysql_config  # noqa: E402

COLLISION_OVERRIDES = {
    "鲑鱼粉": "STD00072",
    "鸡胸肉": "STD00215",
    "鱼油(鳀鱼油)": "STD00861",
}
STANDARD_MERGES = {"STD00409": "STD00572"}
EXCLUDED_ALIASES = {"鱼油(鳀鱼油)", "鱼油（鳀鱼油）"}
NEW_STANDARDS = {
    "橄榄油": ("STD00862", "植物油脂类", "plant", None, "脂肪供给"),
    "鸡油": ("STD00863", "动物油脂类", "animal", "鸡", "脂肪供给"),
    "冻牡蛎": ("STD00864", "鱼类/海洋蛋白类", "animal", "牡蛎", "蛋白质供给"),
    "软骨素": ("STD00865", "复合添加剂类", "synthetic", None, "关节支持"),
}


def split_aliases(value):
    result, buffer, depth = [], [], 0
    for char in str(value or ""):
        if char in "([（【":
            depth += 1
        elif char in ")]）】" and depth:
            depth -= 1
        if char == "、" and depth == 0:
            item = "".join(buffer).strip()
            if item:
                result.append(item)
            buffer = []
        else:
            buffer.append(char)
    item = "".join(buffer).strip()
    if item:
        result.append(item)
    return result


def load_workbook_data(path: Path):
    payload = json.loads(path.read_text(encoding="utf-8"))
    matrix = payload["Sheet4"]
    headers = matrix[0]
    rows = [dict(zip(headers, row)) for row in matrix[1:] if any(v not in (None, "") for v in row)]
    by_id = defaultdict(lambda: {"names": set(), "aliases": set()})
    for row in rows:
        name = str(row.get("standard_name") or "").strip()
        standard_id = str(row.get("standard_ingredient_id") or "").strip()
        if not standard_id and name in NEW_STANDARDS:
            standard_id = NEW_STANDARDS[name][0]
        if not standard_id or not name:
            raise ValueError(f"invalid workbook row: {row}")
        by_id[standard_id]["names"].add(name)
        by_id[standard_id]["aliases"].add(name)
        by_id[standard_id]["aliases"].update(split_aliases(row.get("alias_names")))
    return rows, by_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-json", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    rows, by_id = load_workbook_data(args.input_json)
    for old_id, target_id in STANDARD_MERGES.items():
        if old_id in by_id:
            by_id[target_id]["names"].update(by_id[old_id]["names"])
            by_id[target_id]["aliases"].update(by_id[old_id]["aliases"])
            del by_id[old_id]
    conn = pymysql.connect(**get_mysql_config(), cursorclass=pymysql.cursors.DictCursor, autocommit=False)
    with conn, conn.cursor() as cursor:
        cursor.execute("SELECT * FROM catfood_standard_ingredient")
        standards = {row["standard_ingredient_id"]: row for row in cursor.fetchall()}
        missing_ids = sorted(set(by_id) - set(standards))
        allowed_new_ids = {value[0] for value in NEW_STANDARDS.values()}
        unexpected_missing = sorted(set(missing_ids) - allowed_new_ids)
        if unexpected_missing:
            raise ValueError(f"workbook standard IDs missing in database: {unexpected_missing}")
        canonical = {}
        for standard_id, group in by_id.items():
            current_name = str((standards.get(standard_id) or {}).get("standard_name") or "").strip()
            canonical[standard_id] = current_name if current_name in group["aliases"] else sorted(group["names"])[0]

        alias_targets = defaultdict(set)
        for standard_id, group in by_id.items():
            for alias in group["aliases"]:
                if alias:
                    alias_targets[alias].add(standard_id)
        for excluded_alias in EXCLUDED_ALIASES:
            alias_targets.pop(excluded_alias, None)
        unresolved = {
            alias: sorted(targets)
            for alias, targets in alias_targets.items()
            if len(targets) > 1 and alias not in COLLISION_OVERRIDES
        }
        invalid_overrides = {
            alias: target for alias, target in COLLISION_OVERRIDES.items()
            if alias in alias_targets and target not in by_id
        }
        if unresolved or invalid_overrides:
            raise ValueError(json.dumps({"unresolved": unresolved, "invalid_overrides": invalid_overrides}, ensure_ascii=False))

        final_aliases = set()
        for alias, targets in alias_targets.items():
            target = COLLISION_OVERRIDES.get(alias)
            if not target:
                if len(targets) != 1:
                    continue
                target = next(iter(targets))
            final_aliases.add((target, alias))

        backups, deactivated, activated, renamed, inserted_aliases = [], 0, 0, 0, 0
        if args.apply:
            suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
            for table in ("catfood_standard_ingredient", "catfood_standard_ingredient_alias",
                          "catfood_formula_ingredient_item", "catfood_standard_ingredient_candidate",
                          "catfood_formula_feature_profile"):
                backup = f"{table[:35]}_ar_{suffix}"
                cursor.execute(f"CREATE TABLE `{backup}` LIKE `{table}`")
                cursor.execute(f"INSERT INTO `{backup}` SELECT * FROM `{table}`")
                backups.append(backup)
            cursor.execute("SHOW COLUMNS FROM catfood_standard_ingredient_alias")
            alias_columns = {row["Field"] for row in cursor.fetchall()}
            if "created_at" not in alias_columns:
                cursor.execute(
                    "ALTER TABLE catfood_standard_ingredient_alias "
                    "ADD COLUMN created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP"
                )
            if "updated_at" not in alias_columns:
                cursor.execute(
                    "ALTER TABLE catfood_standard_ingredient_alias "
                    "ADD COLUMN updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP "
                    "ON UPDATE CURRENT_TIMESTAMP"
                )
            for name, (standard_id, family, source_type, animal_source, role) in NEW_STANDARDS.items():
                if standard_id not in standards:
                    cursor.execute(
                        """INSERT INTO catfood_standard_ingredient(
                          standard_ingredient_id,standard_name,ingredient_family,source_type,
                          animal_source,primary_nutrition_role,active)
                          VALUES(%s,%s,%s,%s,%s,%s,1)""",
                        (standard_id, name, family, source_type, animal_source, role),
                    )
            placeholders = ",".join(["%s"] * len(by_id))
            cursor.execute(
                f"UPDATE catfood_standard_ingredient SET active=0,updated_at=NOW() "
                f"WHERE standard_ingredient_id NOT IN ({placeholders}) AND active<>0",
                tuple(sorted(by_id)),
            )
            deactivated = cursor.rowcount
            for standard_id, name in canonical.items():
                cursor.execute(
                    "UPDATE catfood_standard_ingredient SET standard_name=%s,active=1,updated_at=NOW() "
                    "WHERE standard_ingredient_id=%s",
                    (name, standard_id),
                )
                activated += 1
                if str((standards.get(standard_id) or {}).get("standard_name") or "").strip() != name:
                    renamed += 1
            cursor.execute("DELETE FROM catfood_standard_ingredient_alias")
            grouped_final_aliases = defaultdict(list)
            for standard_id, alias in sorted(final_aliases):
                grouped_final_aliases[standard_id].append(alias)
            for standard_id in sorted(by_id):
                cursor.execute(
                    """INSERT INTO catfood_standard_ingredient_alias(
                      standard_ingredient_id,standard_name,alias_names)
                      VALUES(%s,%s,%s)""",
                    (standard_id, canonical[standard_id], "、".join(grouped_final_aliases[standard_id])),
                )
                inserted_aliases += len(grouped_final_aliases[standard_id])
            conn.commit()
        else:
            conn.rollback()
        print(json.dumps({
            "applied": args.apply, "workbook_rows": len(rows), "active_standard_ids": len(by_id),
            "final_aliases": len(final_aliases), "deactivated_standards": deactivated,
            "activated_standards": activated, "renamed_standards": renamed,
            "inserted_aliases": inserted_aliases, "collision_overrides": COLLISION_OVERRIDES,
            "standard_merges": STANDARD_MERGES,
            "excluded_aliases": sorted(EXCLUDED_ALIASES),
            "new_standard_ids": missing_ids,
            "backups": backups,
        }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
