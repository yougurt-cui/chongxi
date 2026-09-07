#!/usr/bin/env python
"""Import cat-food brand and SKU rankings from the supplied XLSX workbook."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pymysql

BASE_DIR = Path(__file__).resolve().parents[1]
import sys

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app_config import get_mysql_config


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _column_index(cell_ref: str) -> int:
    letters = re.match(r"[A-Z]+", cell_ref)
    if not letters:
        raise ValueError(f"Invalid cell reference: {cell_ref}")
    result = 0
    for char in letters.group(0):
        result = result * 26 + ord(char) - 64
    return result - 1


def read_xlsx(path: Path) -> dict[str, list[dict[str, str]]]:
    with ZipFile(path) as archive:
        shared_root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
        shared_strings = [
            "".join(node.text or "" for node in item.iter(f"{{{MAIN_NS}}}t"))
            for item in shared_root.findall(f"{{{MAIN_NS}}}si")
        ]
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {item.attrib["Id"]: item.attrib["Target"] for item in relationships}

        sheets: dict[str, list[dict[str, str]]] = {}
        for sheet in workbook.find(f"{{{MAIN_NS}}}sheets") or []:
            name = sheet.attrib["name"]
            target = targets[sheet.attrib[f"{{{REL_NS}}}id"]]
            xml_path = target if target.startswith("xl/") else f"xl/{target.lstrip('/')}"
            root = ET.fromstring(archive.read(xml_path))
            matrix: list[list[str]] = []
            for row in root.findall(f".//{{{MAIN_NS}}}sheetData/{{{MAIN_NS}}}row"):
                values: list[str] = []
                for cell in row.findall(f"{{{MAIN_NS}}}c"):
                    index = _column_index(cell.attrib["r"])
                    while len(values) <= index:
                        values.append("")
                    value_node = cell.find(f"{{{MAIN_NS}}}v")
                    if value_node is None:
                        value = ""
                    elif cell.attrib.get("t") == "s":
                        value = shared_strings[int(value_node.text or "0")]
                    else:
                        value = value_node.text or ""
                    values[index] = value.strip()
                matrix.append(values)

            if len(matrix) < 2:
                raise ValueError(f"Sheet {name} does not contain a header row")
            headers = matrix[1]
            width = max(index for index, value in enumerate(headers) if value) + 1
            headers = headers[:width]
            sheets[name] = [
                dict(zip(headers, row[:width] + [""] * max(0, width - len(row))))
                for row in matrix[2:]
                if any(row[:width])
            ]
        return sheets


def _nullable(value: str) -> str | None:
    return value or None


def _nullable_int(value: str) -> int | None:
    return int(value) if value else None


def import_rankings(workbook_path: Path) -> tuple[int, int]:
    sheets = read_xlsx(workbook_path)
    brand_rows = sheets.get("品牌表")
    sku_rows = sheets.get("SKU表")
    if brand_rows is None or sku_rows is None:
        raise ValueError("Workbook must contain 品牌表 and SKU表")

    expected_brand_headers = {
        "BRAND_ID", "标准品牌", "英文/别名", "市场覆盖层级", "公开证据渠道",
        "证据等级", "SKU样本数", "A级SKU数", "是否有当前SKU证据", "最近公开证据说明",
    }
    expected_sku_headers = {
        "SKU_ID", "品牌", "SKU/产品名", "规格", "生命阶段", "形态/工艺",
        "功能标签", "平台", "来源网站", "榜单/来源", "统计周期", "榜内排名",
    }
    if set(brand_rows[0]) != expected_brand_headers:
        raise ValueError("品牌表字段与预期不一致")
    if set(sku_rows[0]) != expected_sku_headers:
        raise ValueError("SKU表字段与预期不一致")
    if len({row["BRAND_ID"] for row in brand_rows}) != len(brand_rows):
        raise ValueError("品牌表存在重复 BRAND_ID")
    if len({row["SKU_ID"] for row in sku_rows}) != len(sku_rows):
        raise ValueError("SKU表存在重复 SKU_ID")

    config = get_mysql_config(database="csv_labeling")
    connection = pymysql.connect(
        **config, cursorclass=pymysql.cursors.DictCursor, autocommit=False
    )
    source_file = workbook_path.name
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS catfood_brand_ranking (
                    brand_id VARCHAR(16) NOT NULL,
                    standard_brand VARCHAR(255) NOT NULL,
                    english_name_or_alias VARCHAR(255) NULL,
                    market_coverage_tier VARCHAR(128) NOT NULL,
                    public_evidence_channel VARCHAR(255) NOT NULL,
                    evidence_grade VARCHAR(32) NOT NULL,
                    sku_sample_count INT UNSIGNED NOT NULL DEFAULT 0,
                    grade_a_sku_count INT UNSIGNED NOT NULL DEFAULT 0,
                    has_current_sku_evidence TINYINT(1) NOT NULL DEFAULT 0,
                    latest_public_evidence_note VARCHAR(512) NOT NULL,
                    source_file VARCHAR(255) NOT NULL,
                    imported_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (brand_id),
                    KEY idx_brand_ranking_standard_brand (standard_brand),
                    KEY idx_brand_ranking_coverage_tier (market_coverage_tier)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS catfood_sku_ranking (
                    sku_id VARCHAR(16) NOT NULL,
                    brand VARCHAR(255) NOT NULL,
                    product_name VARCHAR(512) NOT NULL,
                    specification VARCHAR(64) NULL,
                    life_stage VARCHAR(64) NOT NULL,
                    form_or_process VARCHAR(128) NOT NULL,
                    function_tags VARCHAR(255) NULL,
                    platform VARCHAR(64) NOT NULL,
                    source_website VARCHAR(255) NOT NULL,
                    ranking_source VARCHAR(255) NOT NULL,
                    statistical_period VARCHAR(64) NOT NULL,
                    rank_in_list INT UNSIGNED NULL,
                    source_file VARCHAR(255) NOT NULL,
                    imported_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (sku_id),
                    KEY idx_sku_ranking_brand (brand),
                    KEY idx_sku_ranking_period (statistical_period),
                    KEY idx_sku_ranking_source_rank (ranking_source, rank_in_list)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
                """
            )

            brand_values = [
                (
                    row["BRAND_ID"], row["标准品牌"], _nullable(row["英文/别名"]),
                    row["市场覆盖层级"], row["公开证据渠道"], row["证据等级"],
                    int(row["SKU样本数"]), int(row["A级SKU数"]),
                    1 if row["是否有当前SKU证据"] == "是" else 0,
                    row["最近公开证据说明"], source_file,
                )
                for row in brand_rows
            ]
            cursor.executemany(
                """
                INSERT INTO catfood_brand_ranking (
                    brand_id, standard_brand, english_name_or_alias, market_coverage_tier,
                    public_evidence_channel, evidence_grade, sku_sample_count,
                    grade_a_sku_count, has_current_sku_evidence,
                    latest_public_evidence_note, source_file
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                    standard_brand=VALUES(standard_brand),
                    english_name_or_alias=VALUES(english_name_or_alias),
                    market_coverage_tier=VALUES(market_coverage_tier),
                    public_evidence_channel=VALUES(public_evidence_channel),
                    evidence_grade=VALUES(evidence_grade),
                    sku_sample_count=VALUES(sku_sample_count),
                    grade_a_sku_count=VALUES(grade_a_sku_count),
                    has_current_sku_evidence=VALUES(has_current_sku_evidence),
                    latest_public_evidence_note=VALUES(latest_public_evidence_note),
                    source_file=VALUES(source_file)
                """,
                brand_values,
            )

            sku_values = [
                (
                    row["SKU_ID"], row["品牌"], row["SKU/产品名"], _nullable(row["规格"]),
                    row["生命阶段"], row["形态/工艺"], _nullable(row["功能标签"]),
                    row["平台"], row["来源网站"], row["榜单/来源"], row["统计周期"],
                    _nullable_int(row["榜内排名"]), source_file,
                )
                for row in sku_rows
            ]
            cursor.executemany(
                """
                INSERT INTO catfood_sku_ranking (
                    sku_id, brand, product_name, specification, life_stage, form_or_process,
                    function_tags, platform, source_website, ranking_source,
                    statistical_period, rank_in_list, source_file
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                    brand=VALUES(brand), product_name=VALUES(product_name),
                    specification=VALUES(specification), life_stage=VALUES(life_stage),
                    form_or_process=VALUES(form_or_process), function_tags=VALUES(function_tags),
                    platform=VALUES(platform), source_website=VALUES(source_website),
                    ranking_source=VALUES(ranking_source), statistical_period=VALUES(statistical_period),
                    rank_in_list=VALUES(rank_in_list), source_file=VALUES(source_file)
                """,
                sku_values,
            )
        connection.commit()
        return len(brand_values), len(sku_values)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("workbook", type=Path)
    args = parser.parse_args()
    brand_count, sku_count = import_rankings(args.workbook.resolve())
    print(f"Imported {brand_count} brand rows and {sku_count} SKU rows")


if __name__ == "__main__":
    main()
