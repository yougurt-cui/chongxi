#!/usr/bin/env python3
"""Create editable enum-score and business-threshold configuration tables."""

from __future__ import annotations

import sys
from pathlib import Path

import pymysql

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app_config import get_mysql_config  # noqa: E402

ENUM_ROWS = [
    # domain, dimension, enum, level, score, min, max, note
    *(('protein', 'meat_source_complexity', v, i, float(i), 0, 5, '蛋白压力') for v, i in
      [('单一来源',1),('同类双源',2),('同类多源',3),('跨类双源',4),('跨类多源',5)]),
    *(('protein', 'main_protein_form', v, i, s, 0, 2, '蛋白压力') for v, i, s in
      [('水解蛋白为主',1,.5),('鲜肉为主',2,1),('鲜肉/冻肉为主',3,1.25),('冻肉为主',4,1.5),('肉粉为主',5,2)]),
    *(('protein', 'secondary_protein_form', v, i, s, 0, 1, '蛋白压力') for v, i, s in
      [('无',1,0),('水解蛋白',1,0),('鲜肉',2,.5),('冻肉',2,.5),('鲜肉/冻肉',2,.5),('肉粉',3,1)]),
    *(('protein', 'plant_protein_interference', v, i, s, 0, 1, '蛋白压力') for v, i, s in
      [('无植物蛋白',1,0),('1级｜单一温和型植物蛋白',2,.25),('2级｜单一高浓缩型植物蛋白',3,.5),('3级｜多源植物蛋白补强型',4,.75),('4级｜豆类主导混合型植物蛋白',5,1)]),
    *(('protein', 'hydrolyzed_protein_relief', v, i, s, 0, 1, '水解缓释') for v, i, s in
      [('无',1,0),('次要出现',2,.5),('主要出现',3,1)]),
    *(('fiber', 'fermentability_q_feed', v, i, s, 0, 1, '供菌') for v, i, s in
      [('低',1,.1),('中低',2,.3),('中',3,.5),('高',4,.8)]),
    *(('fiber', 'fermentability_q_scfa', v, i, s, 0, 1, 'SCFA支持') for v, i, s in
      [('低',1,0),('中低',2,.2),('中',3,.4),('高',4,.6)]),
    *(('carb', 'starch_category', v, i, s, 0, 2, '碳水负担') for v, i, s in
      [('豆类碳水来源',1,1.2),('谷物淀粉来源',2,1.3),('薯类淀粉来源',3,1.5),('高淀粉粉类',4,1.8),('精制淀粉/纯淀粉',5,2.0)]),
    *(('fat', dimension, '固定区间', 0, None, lower, upper, '固定归一化区间') for dimension, (lower, upper) in {
      'animal_fat_load':(0,4),'plant_fat_interference':(0,3),'fish_oil_load':(0,2),
      'fish_source':(0,2),'antioxidant_protection':(0,4),'micronutrient_support':(0,3),
      'omega3':(0,4),'omega6_animal':(0,4),'omega6_plant':(0,3),'omega6':(0,4),
      'fat_mix_complexity':(0,3),
    }.items()),
]

THRESHOLDS = [('low_upper',40,'中低上限'),('support_min',60,'支持较好下限'),
              ('elevated_min',60,'负担偏高下限'),('high_min',80,'高风险下限')]
POSITION_WEIGHTS = [
    ('global', 1, 1, 1.2, 0, '第1位'),
    ('global', 2, 3, 1.0, 0, '第2-3位'),
    ('global', 4, 5, 0.8, 0, '第4-5位'),
    ('global', 6, 8, 0.6, 0, '第6-8位'),
    ('global', 9, 12, 0.4, 0, '第9-12位'),
    ('global', 13, 9999, 0.2, 0, '第13位以后'),
    ('global', None, None, 0.5, 1, '位置未知'),
]


def initialize() -> dict:
    cfg = get_mysql_config()
    with pymysql.connect(**cfg, cursorclass=pymysql.cursors.DictCursor, autocommit=False) as conn:
        with conn.cursor() as cursor:
            cursor.execute("""CREATE TABLE IF NOT EXISTS catfood_score_enum_config(
              config_id BIGINT PRIMARY KEY AUTO_INCREMENT, domain_code VARCHAR(32) NOT NULL,
              dimension_code VARCHAR(64) NOT NULL, enum_value VARCHAR(255) NOT NULL,
              enum_level INT NOT NULL, score_value DECIMAL(12,6) NULL,
              range_min DECIMAL(12,6) NULL, range_max DECIMAL(12,6) NULL,
              config_version VARCHAR(32) NOT NULL DEFAULT 'v1', active TINYINT NOT NULL DEFAULT 1,
              note VARCHAR(255) NULL, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
              UNIQUE KEY uq_enum_config(domain_code,dimension_code,enum_value,config_version)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS catfood_score_threshold_config(
              threshold_code VARCHAR(64) PRIMARY KEY, threshold_value DECIMAL(12,6) NOT NULL,
              score_scale VARCHAR(16) NOT NULL DEFAULT '0-100', active TINYINT NOT NULL DEFAULT 1,
              note VARCHAR(255), updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS catfood_score_position_weight_config(
              config_id BIGINT PRIMARY KEY AUTO_INCREMENT, domain_code VARCHAR(32) NOT NULL DEFAULT 'global',
              rank_start INT NULL, rank_end INT NULL, position_weight DECIMAL(12,6) NOT NULL,
              is_unknown TINYINT NOT NULL DEFAULT 0, config_version VARCHAR(32) NOT NULL DEFAULT 'v1',
              active TINYINT NOT NULL DEFAULT 1, note VARCHAR(255),
              updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
              UNIQUE KEY uq_position_config(domain_code,rank_start,rank_end,is_unknown,config_version)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
            cursor.executemany("""INSERT INTO catfood_score_enum_config
              (domain_code,dimension_code,enum_value,enum_level,score_value,range_min,range_max,note)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE
              enum_level=VALUES(enum_level),score_value=VALUES(score_value),range_min=VALUES(range_min),
              range_max=VALUES(range_max),note=VALUES(note)""", ENUM_ROWS)
            cursor.executemany("""INSERT INTO catfood_score_threshold_config
              (threshold_code,threshold_value,note) VALUES(%s,%s,%s) ON DUPLICATE KEY UPDATE
              threshold_value=VALUES(threshold_value),note=VALUES(note)""", THRESHOLDS)
            cursor.execute("DELETE FROM catfood_score_position_weight_config WHERE domain_code='global' AND config_version='v1'")
            cursor.executemany("""INSERT INTO catfood_score_position_weight_config
              (domain_code,rank_start,rank_end,position_weight,is_unknown,note)
              VALUES(%s,%s,%s,%s,%s,%s)""", POSITION_WEIGHTS)
        conn.commit()
    return {'enum_rows': len(ENUM_ROWS), 'threshold_rows': len(THRESHOLDS),
            'position_weight_rows': len(POSITION_WEIGHTS)}


if __name__ == '__main__':
    print(initialize())
