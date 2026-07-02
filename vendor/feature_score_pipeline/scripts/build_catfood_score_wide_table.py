# -*- coding: utf-8 -*-
import os
import pandas as pd
from sqlalchemy import Numeric, create_engine, text

from brand_normalizer import build_product_key, correct_brand


DB_USER = os.getenv("MYSQL_USER", "root")
DB_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
DB_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
DB_PORT = os.getenv("MYSQL_PORT", "3306")
DB_NAME = os.getenv("MYSQL_DATABASE", "protein_feature_platform")

PROTEIN_TABLE = "protein_business_cluster_product_details_scored"
PARSED_SOURCE_TABLE = "csv_labeling.catfood_formula_feature_input"
FAT_TABLE = "catfood_fat_material_features_scored"
FIBER_TABLE = "catfood_fiber_feature_score"
OUTPUT_TABLE = "catfood_protein_fat_fiber_score_wide"
OUTPUT_IF_EXISTS = os.getenv("WIDE_SCORE_IF_EXISTS", "append")
SCORE_DECIMALS = 3


def get_engine():
    return create_engine(
        f"mysql+pymysql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}?charset=utf8mb4"
    )


def round_score_columns(frame):
    rounded = frame.copy()
    score_cols = [
        "protein_structure_score",
        "protein_quality_score",
        "fat_oily_score",
        "fat_regulation_score",
        "fat_score",
        "omega_imbalance_score",
        "fat_mix_complexity_score",
        "p_form_score",
        "p_bulk_score",
        "p_buffer",
        "p_total_score",
        "q_feed",
        "q_scfa",
        "q_total_score",
        "starch_burden_score",
    ]
    for col in score_cols:
        if col in rounded.columns:
            rounded[col] = pd.to_numeric(rounded[col], errors="coerce").round(SCORE_DECIMALS)
    return rounded


def normalize_product_key(series):
    return (
        series.astype(str)
        .str.strip()
        .str.replace(r"\s+", " ", regex=True)
        .str.replace("（", "(", regex=False)
        .str.replace("）", ")", regex=False)
        .str.upper()
    )


def quote_identifier(name):
    return "`{}`".format(str(name).replace("`", "``"))


def table_exists(engine, table_name):
    with engine.connect() as conn:
        return bool(
            conn.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM INFORMATION_SCHEMA.TABLES
                    WHERE TABLE_SCHEMA = :schema_name
                      AND TABLE_NAME = :table_name
                    """
                ),
                {"schema_name": DB_NAME, "table_name": table_name},
            ).scalar()
        )


def ensure_wide_unique_key(engine):
    with engine.begin() as conn:
        idx_count = conn.execute(
            text(
                """
                SELECT COUNT(*)
                FROM INFORMATION_SCHEMA.STATISTICS
                WHERE TABLE_SCHEMA = :schema_name
                  AND TABLE_NAME = :table_name
                  AND INDEX_NAME = 'uq_source_id'
                """
            ),
            {"schema_name": DB_NAME, "table_name": OUTPUT_TABLE},
        ).scalar()
        if not idx_count:
            conn.execute(
                text(
                    "ALTER TABLE {} ADD UNIQUE KEY uq_source_id (source_id)".format(
                        quote_identifier(OUTPUT_TABLE)
                    )
                )
            )
        formula_idx_count = conn.execute(
            text(
                """
                SELECT COUNT(*)
                FROM INFORMATION_SCHEMA.STATISTICS
                WHERE TABLE_SCHEMA = :schema_name
                  AND TABLE_NAME = :table_name
                  AND INDEX_NAME = 'uq_formula_id'
                """
            ),
            {"schema_name": DB_NAME, "table_name": OUTPUT_TABLE},
        ).scalar()
        if not formula_idx_count:
            conn.execute(
                text(
                    """
                    DELETE older
                    FROM catfood_protein_fat_fiber_score_wide older
                    JOIN catfood_protein_fat_fiber_score_wide newer
                      ON older.formula_id = newer.formula_id
                     AND older.formula_id IS NOT NULL
                     AND older.source_id < newer.source_id
                    """
                )
            )
            conn.execute(
                text(
                    "ALTER TABLE {} ADD UNIQUE KEY uq_formula_id (formula_id)".format(
                        quote_identifier(OUTPUT_TABLE)
                    )
                )
            )


def write_wide_incremental(engine, wide_df, score_types):
    if OUTPUT_IF_EXISTS in {"replace", "fail"} or not table_exists(engine, OUTPUT_TABLE):
        with engine.begin() as conn:
            wide_df.to_sql(
                OUTPUT_TABLE,
                conn,
                if_exists=OUTPUT_IF_EXISTS if table_exists(engine, OUTPUT_TABLE) else "replace",
                index=False,
                dtype=score_types,
            )
        ensure_wide_unique_key(engine)
        return

    ensure_wide_unique_key(engine)
    temp_table = "__tmp_{}_{}".format(OUTPUT_TABLE, os.getpid())
    quoted_temp = quote_identifier(temp_table)
    quoted_output = quote_identifier(OUTPUT_TABLE)
    columns = list(wide_df.columns)
    insert_columns = ", ".join(quote_identifier(col) for col in columns)
    select_columns = ", ".join(quote_identifier(col) for col in columns)
    update_columns = [col for col in columns if col not in {"source_id", "formula_id"}]
    update_sql = ", ".join(
        "{0}=VALUES({0})".format(quote_identifier(col)) for col in update_columns
    )

    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS {}".format(quoted_temp)))
        wide_df.to_sql(
            temp_table,
            conn,
            if_exists="replace",
            index=False,
            dtype=score_types,
        )
        conn.execute(
            text(
                """
                INSERT INTO {output} ({insert_columns})
                SELECT {select_columns}
                FROM {temp}
                ON DUPLICATE KEY UPDATE {update_sql}
                """.format(
                    output=quoted_output,
                    insert_columns=insert_columns,
                    select_columns=select_columns,
                    temp=quoted_temp,
                    update_sql=update_sql,
                )
            )
        )
        if not os.getenv("FORMULA_ID"):
            conn.execute(
                text(
                    """
                    DELETE output
                    FROM {output} AS output
                    LEFT JOIN {temp} AS latest
                      ON output.formula_id = latest.formula_id
                    WHERE latest.formula_id IS NULL
                    """.format(output=quoted_output, temp=quoted_temp)
                )
            )
        conn.execute(text("DROP TABLE IF EXISTS {}".format(quoted_temp)))


def main():
    engine = get_engine()
    formula_id = int(os.environ["FORMULA_ID"]) if os.getenv("FORMULA_ID") else None

    protein_df = pd.read_sql(
        f"""
        SELECT
            protein.formula_id,
            protein.source_id,
            protein.product_key,
            protein.brand_name AS brand,
            protein.product_name,
            parsed.image_name,
            parsed.image_path,
            protein.protein_structure_score,
            protein.protein_quality_score,
            NULL AS business_label
        FROM {PROTEIN_TABLE} AS protein
        INNER JOIN {PARSED_SOURCE_TABLE} AS parsed
          ON parsed.formula_id = protein.formula_id
        INNER JOIN csv_labeling.catfood_formula_feature_profile AS gate
          ON gate.formula_id = protein.formula_id
         AND gate.overall_status = 'ready_for_rebuild'
        """,
        engine,
    )

    fat_df = pd.read_sql(
        f"""
        SELECT
            formula_id,
            product_key,
            fat_structure_type,
            fat_oily_score,
            NULL AS fat_oily_level,
            fat_regulation_score,
            NULL AS fat_regulation_level,
            fat_score,
            omega_imbalance_score,
            fat_mix_complexity_score,
            fat_reason_tags,
            NULL AS business_name,
            NULL AS combo_profile
        FROM {FAT_TABLE}
        """,
        engine,
    )

    fiber_df = pd.read_sql(
        f"""
        SELECT
            formula_id,
            product_key,
            p_form_score,
            p_bulk_score,
            p_buffer_score AS p_buffer,
            p_total_score,
            p_level,
            q_feed_score AS q_feed,
            q_scfa_score AS q_scfa,
            q_total_score,
            q_level,
            starch_burden_score
        FROM {FIBER_TABLE}
        """,
        engine,
    )
    if formula_id is not None:
        protein_df = protein_df[protein_df["formula_id"] == formula_id].copy()
        fat_df = fat_df[fat_df["formula_id"] == formula_id].copy()
        fiber_df = fiber_df[fiber_df["formula_id"] == formula_id].copy()

    linked_protein = (
        protein_df[protein_df["formula_id"].notna()]
        .sort_values("source_id")
        .drop_duplicates(subset=["formula_id"], keep="last")
    )
    protein_df = pd.concat(
        [linked_protein, protein_df[protein_df["formula_id"].isna()]],
        ignore_index=True,
    )
    fat_df = fat_df.drop_duplicates(subset=["formula_id"], keep="first")
    fiber_df = fiber_df.drop_duplicates(subset=["formula_id"], keep="first")

    protein_df["product_key_join"] = normalize_product_key(protein_df["product_key"])
    fat_df["product_key_join"] = normalize_product_key(fat_df["product_key"])
    fiber_df["product_key_join"] = normalize_product_key(fiber_df["product_key"])

    fat_df = fat_df.drop_duplicates(subset=["product_key_join"], keep="first")
    fiber_df = fiber_df.drop_duplicates(subset=["product_key_join"], keep="first")

    wide_df = protein_df.merge(
        fat_df.drop(columns=["product_key", "product_key_join"]),
        on="formula_id",
        how="left",
    )
    wide_df = wide_df.merge(
        fiber_df.drop(columns=["product_key", "product_key_join"]),
        on="formula_id",
        how="left",
    )

    wide_df = wide_df[
        [
            "formula_id",
            "source_id",
            "product_key",
            "brand",
            "product_name",
            "image_name",
            "image_path",
            "protein_structure_score",
            "protein_quality_score",
            "business_label",
            "fat_structure_type",
            "fat_oily_score",
            "fat_oily_level",
            "fat_regulation_score",
            "fat_regulation_level",
            "fat_score",
            "omega_imbalance_score",
            "fat_mix_complexity_score",
            "fat_reason_tags",
            "business_name",
            "combo_profile",
            "p_form_score",
            "p_bulk_score",
            "p_buffer",
            "p_total_score",
            "p_level",
            "q_feed",
            "q_scfa",
            "q_total_score",
            "q_level",
            "starch_burden_score",
        ]
    ].copy()

    wide_df["brand"] = wide_df.apply(
        lambda row: correct_brand(
            row.get("brand"),
            row.get("product_name"),
            row.get("image_name"),
            row.get("image_path"),
        ),
        axis=1,
    )
    wide_df["product_key"] = wide_df.apply(
        lambda row: build_product_key(row.get("brand"), row.get("product_name")),
        axis=1,
    )
    wide_df = wide_df.drop(columns=["image_name", "image_path"], errors="ignore")

    wide_df = round_score_columns(wide_df)
    wide_df = wide_df.sort_values(["brand", "product_name"], na_position="last").reset_index(drop=True)

    score_types = {
        col: Numeric(12, SCORE_DECIMALS)
        for col in [
            "protein_structure_score",
            "protein_quality_score",
            "fat_oily_score",
            "fat_regulation_score",
            "fat_score",
            "omega_imbalance_score",
            "fat_mix_complexity_score",
            "p_form_score",
            "p_bulk_score",
            "p_buffer",
            "p_total_score",
            "q_feed",
            "q_scfa",
            "q_total_score",
            "starch_burden_score",
        ]
    }

    write_wide_incremental(engine, wide_df, score_types)

    print("=" * 80)
    print("宽表生成完成")
    print(f"结果表：{OUTPUT_TABLE}")
    print(f"写入行数：{len(wide_df)}")
    print(f"字段数：{len(wide_df.columns)}")
    print("=" * 80)
    print(wide_df.head(10))


if __name__ == "__main__":
    main()
