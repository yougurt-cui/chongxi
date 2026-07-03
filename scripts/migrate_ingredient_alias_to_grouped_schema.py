#!/usr/bin/env python3
"""Migrate ingredient alias table to one grouped row per standard ingredient."""
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
import pymysql

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))
from app_config import get_mysql_config  # noqa: E402


def main() -> int:
    conn = pymysql.connect(**get_mysql_config(), cursorclass=pymysql.cursors.DictCursor, autocommit=False)
    with conn, conn.cursor() as cursor:
        cursor.execute("SELECT standard_ingredient_id,alias_name FROM catfood_standard_ingredient_alias WHERE active=1 ORDER BY alias_id")
        aliases = defaultdict(list)
        for row in cursor.fetchall():
            value = str(row["alias_name"] or "").strip()
            if value and value not in aliases[row["standard_ingredient_id"]]:
                aliases[row["standard_ingredient_id"]].append(value)
        cursor.execute("SELECT standard_ingredient_id,standard_name FROM catfood_standard_ingredient WHERE active=1 ORDER BY standard_ingredient_id")
        standards = list(cursor.fetchall())
        suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = f"catfood_standard_ingredient_alias_rows_{suffix}"
        cursor.execute(f"CREATE TABLE `{backup}` LIKE catfood_standard_ingredient_alias")
        cursor.execute(f"INSERT INTO `{backup}` SELECT * FROM catfood_standard_ingredient_alias")
        cursor.execute("DROP TABLE IF EXISTS catfood_standard_ingredient_alias_grouped_new")
        cursor.execute(
            """CREATE TABLE catfood_standard_ingredient_alias_grouped_new(
              standard_ingredient_id VARCHAR(64) NOT NULL,
              standard_name VARCHAR(255) NOT NULL,
              alias_names LONGTEXT NULL,
              PRIMARY KEY(standard_ingredient_id),
              KEY idx_grouped_standard_name(standard_name)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
        )
        for row in standards:
            sid, name = row["standard_ingredient_id"], row["standard_name"]
            values = aliases.get(sid, [])
            if name not in values:
                values.insert(0, name)
            cursor.execute(
                "INSERT INTO catfood_standard_ingredient_alias_grouped_new VALUES(%s,%s,%s)",
                (sid, name, "、".join(values)),
            )
        cursor.execute("DROP TABLE catfood_standard_ingredient_alias")
        cursor.execute("RENAME TABLE catfood_standard_ingredient_alias_grouped_new TO catfood_standard_ingredient_alias")
        conn.commit()
    print(json.dumps({"grouped_rows": len(standards), "backup": backup}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
