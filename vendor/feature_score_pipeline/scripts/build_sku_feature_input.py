# -*- coding: utf-8 -*-
import os
from datetime import datetime

import pandas as pd
from sqlalchemy import bindparam, create_engine, text

from brand_normalizer import GENERIC_BRAND_VALUES, build_product_key, correct_brand


DB_USER = os.getenv("MYSQL_USER", "root")
DB_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
DB_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
DB_PORT = os.getenv("MYSQL_PORT", "3306")
DB_NAME = os.getenv("MYSQL_DATABASE", "protein_feature_platform")

SOURCE_TABLE = "catfood_protein_fat_fiber_score_wide"
TARGET_TABLE = "sku_feature_input"

BLACK_CHIN_FEATURE_VERSION = "v1"
SOFT_STOOL_FEATURE_VERSION = "soft_v1"
DEFAULT_BATCH_ID = os.getenv("SKU_FEATURE_BATCH_ID", "current")


def get_engine():
    return create_engine(
        f"mysql+pymysql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}?charset=utf8mb4"
    )


def ensure_target_table(engine):
    ddl = f"""
    CREATE TABLE IF NOT EXISTS {TARGET_TABLE} (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        sku_id VARCHAR(100) NOT NULL,
        sku_name VARCHAR(255) NULL,
        brand_name VARCHAR(100) NULL,
        batch_id VARCHAR(100) NULL,
        data_type VARCHAR(50) NULL,
        feature_version VARCHAR(50) NOT NULL,
        protein_score DECIMAL(10,4) NULL,
        carb_score DECIMAL(10,4) NULL,
        fiber_score DECIMAL(10,4) NULL,
        fat_score DECIMAL(10,4) NULL,
        prebiotic_score DECIMAL(10,4) NULL,
        antioxidant_score DECIMAL(10,4) NULL,
        p_buffer DECIMAL(10,4) NULL,
        q_feed DECIMAL(10,4) NULL,
        q_scfa DECIMAL(10,4) NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uk_sku_feature_version (sku_id, feature_version),
        KEY idx_feature_version (feature_version),
        KEY idx_batch_id (batch_id),
        KEY idx_data_type (data_type)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """
    required_columns = {
        "p_buffer": "ALTER TABLE sku_feature_input ADD COLUMN p_buffer DECIMAL(10,4) NULL AFTER antioxidant_score",
        "q_feed": "ALTER TABLE sku_feature_input ADD COLUMN q_feed DECIMAL(10,4) NULL AFTER p_buffer",
        "q_scfa": "ALTER TABLE sku_feature_input ADD COLUMN q_scfa DECIMAL(10,4) NULL AFTER q_feed",
    }
    with engine.begin() as conn:
        conn.execute(text(ddl))
        existing_cols = {
            row[0]
            for row in conn.execute(
                text(
                    """
                    SELECT COLUMN_NAME
                    FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA = :schema_name
                      AND TABLE_NAME = :table_name
                    """
                ),
                {"schema_name": DB_NAME, "table_name": TARGET_TABLE},
            ).fetchall()
        }
        for column_name, alter_sql in required_columns.items():
            if column_name not in existing_cols:
                conn.execute(text(alter_sql))

        index_count = conn.execute(
            text(
                """
                SELECT COUNT(*)
                FROM INFORMATION_SCHEMA.STATISTICS
                WHERE TABLE_SCHEMA = :schema_name
                  AND TABLE_NAME = :table_name
                  AND INDEX_NAME = 'uk_sku_feature_version'
                """
            ),
            {"schema_name": DB_NAME, "table_name": TARGET_TABLE},
        ).scalar()
        if not index_count:
            conn.execute(
                text(
                    f"ALTER TABLE {TARGET_TABLE} "
                    "ADD UNIQUE KEY uk_sku_feature_version (sku_id, feature_version)"
                )
            )


def load_wide(engine):
    sql = f"""
    SELECT
        product_key,
        brand,
        product_name,
        protein_structure_score,
        fat_score,
        fat_regulation_score,
        p_total_score,
        p_buffer,
        q_feed,
        q_scfa,
        starch_burden_score
    FROM {SOURCE_TABLE}
    WHERE product_key IS NOT NULL
      AND TRIM(product_key) <> ''
    """
    return pd.read_sql(sql, engine)


def numeric_series(frame, column_name):
    if column_name not in frame.columns:
        return pd.Series([0.0] * len(frame), index=frame.index)
    return pd.to_numeric(frame[column_name], errors="coerce").fillna(0.0)


def build_feature_rows(wide_df, *, feature_version, data_type):
    rows = pd.DataFrame(
        {
            "sku_id": wide_df["product_key"].astype(str).str.strip(),
            "sku_name": wide_df["product_name"],
            "brand_name": wide_df["brand"],
            "batch_id": DEFAULT_BATCH_ID,
            "data_type": data_type,
            "feature_version": feature_version,
            "protein_score": numeric_series(wide_df, "protein_structure_score"),
            "carb_score": numeric_series(wide_df, "starch_burden_score"),
            "fiber_score": numeric_series(wide_df, "p_total_score"),
            "fat_score": numeric_series(wide_df, "fat_score"),
            "prebiotic_score": numeric_series(wide_df, "q_feed"),
            "antioxidant_score": numeric_series(wide_df, "fat_regulation_score"),
            "p_buffer": numeric_series(wide_df, "p_buffer"),
            "q_feed": numeric_series(wide_df, "q_feed"),
            "q_scfa": numeric_series(wide_df, "q_scfa"),
        }
    )
    corrected_brand = rows.apply(
        lambda row: correct_brand(row.get("brand_name"), row.get("sku_name")),
        axis=1,
    )
    rows["brand_name"] = corrected_brand
    rows["sku_id"] = rows.apply(
        lambda row: build_product_key(row.get("brand_name"), row.get("sku_name")),
        axis=1,
    )
    rows = rows[rows["sku_id"] != ""].copy()
    rows = rows.drop_duplicates(subset=["sku_id", "feature_version"], keep="first")
    return rows


def upsert_rows(engine, rows):
    if rows.empty:
        return 0

    params = rows.where(pd.notnull(rows), None).to_dict(orient="records")
    sql = text(
        f"""
        INSERT INTO {TARGET_TABLE} (
            sku_id,
            sku_name,
            brand_name,
            batch_id,
            data_type,
            feature_version,
            protein_score,
            carb_score,
            fiber_score,
            fat_score,
            prebiotic_score,
            antioxidant_score,
            p_buffer,
            q_feed,
            q_scfa,
            created_at
        ) VALUES (
            :sku_id,
            :sku_name,
            :brand_name,
            :batch_id,
            :data_type,
            :feature_version,
            :protein_score,
            :carb_score,
            :fiber_score,
            :fat_score,
            :prebiotic_score,
            :antioxidant_score,
            :p_buffer,
            :q_feed,
            :q_scfa,
            NOW()
        )
        ON DUPLICATE KEY UPDATE
            sku_name = VALUES(sku_name),
            brand_name = VALUES(brand_name),
            batch_id = VALUES(batch_id),
            data_type = VALUES(data_type),
            protein_score = VALUES(protein_score),
            carb_score = VALUES(carb_score),
            fiber_score = VALUES(fiber_score),
            fat_score = VALUES(fat_score),
            prebiotic_score = VALUES(prebiotic_score),
            antioxidant_score = VALUES(antioxidant_score),
            p_buffer = VALUES(p_buffer),
            q_feed = VALUES(q_feed),
            q_scfa = VALUES(q_scfa)
        """
    )
    with engine.begin() as conn:
        conn.execute(sql, params)
    return len(params)


def delete_stale_generic_brand_rows(engine, rows):
    if rows.empty:
        return 0

    corrected_rows = rows[["sku_id", "sku_name", "brand_name", "feature_version"]].drop_duplicates()
    params = []
    generic_brands = {str(item).strip() for item in GENERIC_BRAND_VALUES if str(item).strip()}
    for row in corrected_rows.to_dict(orient="records"):
        brand_name = str(row.get("brand_name") or "").strip()
        if not brand_name or brand_name in generic_brands:
            continue
        params.append(
            {
                "sku_id": row["sku_id"],
                "sku_name": row["sku_name"],
                "feature_version": row["feature_version"],
                "generic_brands": tuple(generic_brands),
            }
        )
    if not params:
        return 0

    deleted = 0
    with engine.begin() as conn:
        for param in params:
            result = conn.execute(
                text(
                    f"""
                    DELETE FROM {TARGET_TABLE}
                    WHERE sku_name = :sku_name
                      AND feature_version = :feature_version
                      AND sku_id <> :sku_id
                      AND brand_name IN :generic_brands
                    """
                ).bindparams(bindparam("generic_brands", expanding=True)),
                param,
            )
            deleted += int(result.rowcount or 0)
    return deleted


def main():
    engine = get_engine()
    ensure_target_table(engine)
    wide_df = load_wide(engine)
    if wide_df.empty:
        raise ValueError(f"{SOURCE_TABLE} 没有可用于生成 sku_feature_input 的数据")

    black_rows = build_feature_rows(
        wide_df,
        feature_version=BLACK_CHIN_FEATURE_VERSION,
        data_type="current_black_chin_sku",
    )
    soft_rows = build_feature_rows(
        wide_df,
        feature_version=SOFT_STOOL_FEATURE_VERSION,
        data_type="current_soft_stool_sku",
    )

    deleted_black = delete_stale_generic_brand_rows(engine, black_rows)
    deleted_soft = delete_stale_generic_brand_rows(engine, soft_rows)
    written_black = upsert_rows(engine, black_rows)
    written_soft = upsert_rows(engine, soft_rows)

    print("sku_feature_input 构建完成")
    print(f"来源表: {SOURCE_TABLE}")
    print(f"来源行数: {len(wide_df)}")
    print(f"黑下巴 feature_version={BLACK_CHIN_FEATURE_VERSION}: {written_black}")
    print(f"软便 feature_version={SOFT_STOOL_FEATURE_VERSION}: {written_soft}")
    print(f"清理旧泛化品牌记录: {deleted_black + deleted_soft}")
    print(f"batch_id: {DEFAULT_BATCH_ID}")
    print(f"finished_at: {datetime.now().isoformat(timespec='seconds')}")


if __name__ == "__main__":
    main()
