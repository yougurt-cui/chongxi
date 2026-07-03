#!/usr/bin/env python3
"""Remove 鱼油(鳀鱼油) alias and map the raw phrase to generic 鱼油."""
import json
import sys
from datetime import datetime
from pathlib import Path
import pymysql

BASE_DIR = Path(__file__).resolve().parents[1]
SCRIPT_DIR = BASE_DIR / "vendor" / "feature_score_pipeline" / "scripts"
sys.path[:0] = [str(BASE_DIR), str(SCRIPT_DIR)]
from app_config import get_mysql_config  # noqa: E402
from vendor.feature_score_pipeline.scripts.rebuild_protein_source_aggregate import _split_grouped_alias_names  # noqa: E402


def main() -> int:
    conn = pymysql.connect(**get_mysql_config(), cursorclass=pymysql.cursors.DictCursor, autocommit=False)
    with conn, conn.cursor() as cursor:
        suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
        backups = []
        for table in ("catfood_standard_ingredient_alias", "catfood_standard_ingredient_candidate"):
            backup = f"{table}_fishoil_{suffix}"
            cursor.execute(f"CREATE TABLE `{backup}` LIKE `{table}`")
            cursor.execute(f"INSERT INTO `{backup}` SELECT * FROM `{table}`")
            backups.append(backup)
        cursor.execute("SELECT alias_names FROM catfood_standard_ingredient_alias WHERE standard_ingredient_id='STD00861' FOR UPDATE")
        row = cursor.fetchone()
        aliases = _split_grouped_alias_names(row.get("alias_names") if row else None)
        removed = {"鱼油(鳀鱼油)", "鱼油（鳀鱼油）"}
        kept = [alias for alias in aliases if alias not in removed]
        deleted_aliases = len(aliases) - len(kept)
        cursor.execute(
            "UPDATE catfood_standard_ingredient_alias SET alias_names=%s WHERE standard_ingredient_id='STD00861'",
            ("、".join(kept),),
        )
        cursor.execute(
            """UPDATE catfood_standard_ingredient_candidate
               SET status='approved',suggested_standard_ingredient_id='STD00861',
                   suggested_standard_name='鱼油',reviewer='manual_review',
                   review_note='鱼油(鳀鱼油)按通用鱼油归一',reviewed_at=NOW()
               WHERE raw_name IN ('鱼油(鳀鱼油)','鱼油（鳀鱼油）')"""
        )
        updated_candidates = cursor.rowcount
        conn.commit()
    print(json.dumps({"deleted_aliases": deleted_aliases, "updated_candidates": updated_candidates,
                      "backups": backups}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
