#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
decision_comment_labeler.py

对已经被上游识别为 Decision 的猫粮评论做两层细分类：
1) decision_factor：用户拿什么标准判断
2) decision_result：结果是什么

一级决策标准：
- 功能适配度
- 产品顾虑
- 价格顾虑
- 品牌信任

决策结果：
- 未决
- 倾向
- 已选择
- 放弃
"""

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
import pymysql


BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app_config import get_mysql_config  # noqa: E402


DEFAULT_SOURCE_TABLE = "catfood_choice_comments_filtered_v2"
DEFAULT_TARGET_TABLE = "catfood_decision_comment_labels"
LABEL_VERSION = "decision_comment_rules_v1"
DB_BATCH_SIZE = 1000


DECISION_FACTOR_RULES = {
    "功能适配度": {
        "肠胃/消化适配": [
            r"肠胃", r"玻璃胃", r"消化", r"软便", r"拉稀", r"腹泻",
            r"呕吐", r"吐粮", r"便秘", r"便臭", r"肠胃敏感"
        ],
        "皮肤/毛发适配": [
            r"黑下巴", r"掉毛", r"毛发", r"皮肤", r"油脂",
            r"毛囊炎", r"皮屑", r"毛糙", r"皮脂"
        ],
        "泌尿适配": [
            r"泌尿", r"尿闭", r"结晶", r"结石", r"血尿", r"尿频"
        ],
        "体重管理适配": [
            r"易胖", r"肥胖", r"减肥", r"体重管理", r"控制体重",
            r"低脂", r"减脂", r"饱腹"
        ],
        "低敏/过敏适配": [
            r"低敏", r"过敏", r"食物敏感", r"单一肉源",
            r"单一蛋白", r"不能吃鸡", r"鸡肉过敏", r"避开鸡"
        ],
        "幼猫适配": [
            r"幼猫", r"奶猫", r"小猫", r"\d+\s*个?月", r"半岁"
        ],
        "成猫适配": [
            r"成猫", r"成年猫"
        ],
        "老年猫适配": [
            r"老年猫", r"老猫", r"高龄猫", r"老龄猫"
        ],
        "绝育阶段适配": [
            r"绝育后", r"绝育猫", r"刚绝育", r"绝育期"
        ],
        "品种/个体适配": [
            r"英短", r"美短", r"布偶", r"缅因", r"暹罗",
            r"无毛猫", r"斯芬克斯", r"德文", r"田园猫",
            r"适不适合我家猫", r"适合.*猫吗", r"适配"
        ],
    },

    "产品顾虑": {
        "配方复杂度": [
            r"配方复杂", r"配方太杂", r"原料太多", r"成分太多",
            r"配料太多", r"肉源太多", r"配方简单", r"简配"
        ],
        "肉源结构": [
            r"肉源", r"鸡肉多", r"鱼肉多", r"鸭肉", r"牛肉",
            r"多肉源", r"单一肉源", r"肉粉", r"鲜肉"
        ],
        "营养指标": [
            r"蛋白太高", r"蛋白高", r"蛋白低", r"脂肪太高",
            r"脂肪高", r"脂肪低", r"碳水高", r"碳水低",
            r"粗蛋白", r"粗脂肪", r"钙磷", r"热量"
        ],
        "油脂体验": [
            r"太油", r"很油", r"油腻", r"出油", r"喷油",
            r"油脂高", r"油乎乎", r"怕油", r"担心.*油"
        ],
        "适口性": [
            r"适口性", r"不爱吃", r"怕不吃", r"挑食",
            r"挑嘴", r"拒食", r"爱不爱吃", r"吃不吃"
        ],
        "肠胃耐受": [
            r"会不会软便", r"怕软便", r"会不会拉稀", r"怕拉稀",
            r"会不会吐", r"怕吐", r"耐不耐受", r"肠胃耐受"
        ],
        "原料顾虑": [
            r"原料", r"豆类", r"豌豆", r"谷物", r"玉米",
            r"小麦", r"大豆", r"诱食剂", r"添加剂", r"防腐剂"
        ],
        "工艺/生产": [
            r"膨化", r"烘焙", r"风干", r"冻干", r"工艺",
            r"代工厂", r"工厂", r"哪里生产", r"生产方"
        ],
        "产品稳定性": [
            r"换配方", r"配方变了", r"新版", r"旧版",
            r"批次", r"稳定性", r"品控不稳"
        ],
        "安全/品质": [
            r"品控", r"质量", r"安全", r"翻车", r"问题批次",
            r"安不安全", r"品质"
        ],
        "颗粒/气味/形态": [
            r"颗粒大", r"颗粒小", r"颗粒硬", r"颗粒",
            r"味道重", r"味道大", r"气味", r"粉多", r"碎"
        ],
    },

    "价格顾虑": {
        "价格高": [
            r"太贵", r"贵了", r"价格高", r"有点贵", r"好贵",
            r"买不起", r"吃不起", r"预算不够"
        ],
        "性价比": [
            r"性价比", r"值不值", r"划算", r"不划算",
            r"这个价格", r"同价位"
        ],
        "长期喂养成本": [
            r"长期吃", r"长期喂", r"长期成本", r"一个月要",
            r"每个月", r"长期吃不起", r"喂不起"
        ],
        "活动/促销价格": [
            r"活动价", r"促销", r"打折", r"优惠", r"双十一",
            r"618", r"等活动", r"降价", r"券后"
        ],
    },

    "品牌信任": {
        "品牌口碑": [
            r"口碑", r"牌子大", r"大牌", r"知名品牌",
            r"品牌知名度", r"名气", r"靠谱品牌"
        ],
        "用户评价": [
            r"评论", r"评价", r"反馈", r"很多人说",
            r"都说", r"差评", r"好评", r"测评"
        ],
        "品控信任": [
            r"品控", r"质量稳定", r"批次稳定", r"翻车",
            r"出过问题", r"靠不靠谱"
        ],
        "历史使用经验": [
            r"以前吃过", r"之前吃过", r"一直吃", r"买过",
            r"回购过", r"之前用过", r"比较放心"
        ],
        "工厂/生产信任": [
            r"工厂", r"代工厂", r"汉欧", r"福贝", r"中宠",
            r"哪个厂", r"生产商", r"生产方"
        ],
        "原料来源/透明度": [
            r"原料来源", r"原料哪里", r"来源透明", r"透明度",
            r"溯源", r"供应商", r"配方透明"
        ],
    },
}


DECISION_RESULT_RULES = {
    "已选择": [
        r"最后买了", r"最后选了", r"最终买了", r"最终选了",
        r"已经买了", r"下单了", r"入手了", r"决定买",
        r"决定选", r"还是买了", r"还是选了", r"买的是",
        r"选的是", r"直接买", r"就买这个", r"定了"
    ],
    "放弃": [
        r"不考虑了", r"不买了", r"放弃", r"劝退",
        r"pass", r"拔草", r"算了不买", r"不准备买",
        r"暂时不买", r"不会买", r"排除", r"不选了"
    ],
    "倾向": [
        r"更倾向", r"比较倾向", r"偏向", r"更想买",
        r"更想选", r"感觉.*更好", r"觉得.*更适合",
        r"目前看好", r"目前更喜欢", r"优先考虑"
    ],
    "未决": [
        r"哪个好", r"哪个更好", r"怎么选", r"选哪个",
        r"纠结", r"犹豫", r"拿不定", r"没决定",
        r"还没决定", r"再看看", r"观望", r"考虑中",
        r"不知道选谁", r"求推荐", r"求建议", r"二选一"
    ],
}

RESULT_PRIORITY = {
    "已选择": 4,
    "放弃": 3,
    "倾向": 2,
    "未决": 1,
}


def normalize_text(text):
    if text is None:
        return ""
    try:
        if text != text:
            return ""
    except (TypeError, ValueError):
        pass
    text = str(text).replace("\u3000", " ")
    return re.sub(r"\s+", " ", text).strip()


def unique_keep_order(items):
    return list(dict.fromkeys(items))


def regex_matches(text, patterns):
    hits = []
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.I):
            hit = match.group(0).strip()
            if hit:
                hits.append(hit)
    return unique_keep_order(hits)


def detect_decision_factors(text):
    primary_labels = []
    secondary_labels = []
    matched_keywords = []

    for primary, sub_map in DECISION_FACTOR_RULES.items():
        primary_hit = False

        for secondary, patterns in sub_map.items():
            hits = regex_matches(text, patterns)
            if hits:
                primary_hit = True
                secondary_labels.append(f"{primary}>{secondary}")
                matched_keywords.extend(
                    [f"标准:{primary}>{secondary}:{hit}" for hit in hits]
                )

        if primary_hit:
            primary_labels.append(primary)

    return (
        unique_keep_order(primary_labels),
        unique_keep_order(secondary_labels),
        unique_keep_order(matched_keywords),
    )


def detect_decision_result(text):
    candidates = []
    matched_keywords = []

    for result, patterns in DECISION_RESULT_RULES.items():
        hits = regex_matches(text, patterns)
        if hits:
            candidates.append(result)
            matched_keywords.extend(
                [f"结果:{result}:{hit}" for hit in hits]
            )

    if not candidates:
        # 已被上游判定为 Decision，但没有明显结果词，默认“未决”
        return "未决", ["结果:未决:默认"]

    selected = max(candidates, key=lambda x: RESULT_PRIORITY[x])
    return selected, unique_keep_order(matched_keywords)


def label_decision_comment(text):
    text = normalize_text(text)

    factor_primary, factor_secondary, factor_hits = detect_decision_factors(text)
    decision_result, result_hits = detect_decision_result(text)

    return {
        "decision_factor_primary": factor_primary,
        "decision_factor_secondary": factor_secondary,
        "decision_result": decision_result,
        "matched_keywords": unique_keep_order(factor_hits + result_hits),
    }


def is_decision_row(value):
    value = normalize_text(value)
    if not value:
        return False

    lower = value.lower()
    tokens = re.split(r"[,|;/、，；\s]+", lower)

    return (
        "decision" in tokens
        or "decison" in tokens
        or "决策" in value
    )


def read_input_file(path):
    try:
        import pandas as pd
    except (ImportError, AttributeError) as exc:
        raise RuntimeError("文件模式需要安装完整 pandas：pip install pandas") from exc
    if not hasattr(pd, "read_csv"):
        raise RuntimeError("当前 pandas 安装不完整；文件模式请重新安装 pandas")
    suffix = path.suffix.lower()

    if suffix == ".csv":
        for encoding in ("utf-8-sig", "utf-8", "gb18030"):
            try:
                return pd.read_csv(path, encoding=encoding)
            except UnicodeDecodeError:
                continue
        raise ValueError("CSV 编码无法识别")

    if suffix in (".xlsx", ".xls"):
        return pd.read_excel(path)

    raise ValueError("仅支持 CSV / XLSX / XLS 文件")


def write_output_file(df, path):
    suffix = path.suffix.lower()

    if suffix == ".csv":
        df.to_csv(path, index=False, encoding="utf-8-sig")
    elif suffix == ".xlsx":
        df.to_excel(path, index=False)
    else:
        raise ValueError("输出文件请使用 .csv 或 .xlsx")


def quote_ident(name):
    if not re.fullmatch(r"[A-Za-z0-9_]+", name or ""):
        raise ValueError(f"不安全的表名：{name}")
    return f"`{name}`"


def connect_mysql(cursorclass=pymysql.cursors.DictCursor, autocommit=False):
    return pymysql.connect(
        **get_mysql_config(), cursorclass=cursorclass, autocommit=autocommit
    )


def ensure_target_table(conn, target_table):
    with conn.cursor() as cur:
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {quote_ident(target_table)} (
              id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
              source_comment_id BIGINT NOT NULL,
              source_platform VARCHAR(32) NOT NULL,
              source_table VARCHAR(64) NOT NULL,
              source_record_key VARCHAR(255) NOT NULL,
              external_id VARCHAR(255) NULL,
              comment_text LONGTEXT NOT NULL,
              intent_labels VARCHAR(255) NOT NULL,
              decision_factor_primary VARCHAR(500) NOT NULL DEFAULT '',
              decision_factor_secondary TEXT NOT NULL,
              decision_result VARCHAR(32) NOT NULL,
              decision_matched_keywords TEXT NOT NULL,
              decision_label_json JSON NOT NULL,
              decision_detail_labeled TINYINT(1) NOT NULL DEFAULT 0,
              content_hash VARCHAR(32) NOT NULL DEFAULT '',
              label_version VARCHAR(64) NOT NULL,
              labeled_at DATETIME NOT NULL,
              UNIQUE KEY uq_source_label (source_comment_id, label_version),
              KEY idx_source_record (source_platform, source_table, source_record_key),
              KEY idx_decision_result (decision_result),
              KEY idx_decision_detail (decision_detail_labeled),
              KEY idx_label_version (label_version),
              KEY idx_content_hash (content_hash)
            ) DEFAULT CHARSET=utf8mb4
            """
        )
        # 兼容已存在的旧表：补字段（忽略已存在时报错，errno=1060）
        try:
            cur.execute(
                f"ALTER TABLE {quote_ident(target_table)} "
                f"ADD COLUMN content_hash VARCHAR(32) NOT NULL DEFAULT ''"
            )
        except pymysql.err.OperationalError as exc:
            if exc.args and exc.args[0] != 1060:
                raise
        # 补索引（忽略已存在时报错，errno=1061）
        try:
            cur.execute(
                f"ALTER TABLE {quote_ident(target_table)} "
                f"ADD KEY idx_content_hash (content_hash)"
            )
        except pymysql.err.OperationalError as exc:
            if exc.args and exc.args[0] != 1061:
                raise
        # 回填 content_hash：用 comment_text 的 MD5 填充历史行
        cur.execute(
            f"UPDATE {quote_ident(target_table)} "
            f"SET content_hash = MD5(comment_text) WHERE content_hash = ''"
        )
    conn.commit()


def iter_source_rows(source_table, *, all_rows=False, limit=0):
    conn = connect_mysql(cursorclass=pymysql.cursors.SSDictCursor)
    try:
        sql = f"""
            SELECT id, source_platform, source_table, source_record_key,
                   external_id, comment_text, intent_labels
            FROM {quote_ident(source_table)}
            WHERE comment_text IS NOT NULL AND TRIM(comment_text) <> ''
        """
        params = []
        if not all_rows:
            sql += " AND FIND_IN_SET('Decision', REPLACE(intent_labels, '、', ',')) > 0"
        sql += " ORDER BY id ASC"
        if limit > 0:
            sql += " LIMIT %s"
            params.append(int(limit))
        with conn.cursor() as cur:
            cur.execute(sql, params)
            for row in cur:
                yield row
    finally:
        conn.close()


def build_db_row(row):
    result = label_decision_comment(row.get("comment_text"))
    return {
        "source_comment_id": row.get("id"),
        "source_platform": normalize_text(row.get("source_platform")),
        "source_table": normalize_text(row.get("source_table")),
        "source_record_key": normalize_text(row.get("source_record_key")),
        "external_id": normalize_text(row.get("external_id")) or None,
        "comment_text": normalize_text(row.get("comment_text")),
        "content_hash": hashlib.md5(normalize_text(row.get("comment_text")).encode("utf-8")).hexdigest(),
        "intent_labels": normalize_text(row.get("intent_labels")),
        "decision_factor_primary": " | ".join(result["decision_factor_primary"]),
        "decision_factor_secondary": " | ".join(result["decision_factor_secondary"]),
        "decision_result": result["decision_result"],
        "decision_matched_keywords": " | ".join(result["matched_keywords"]),
        "decision_label_json": json.dumps(result, ensure_ascii=False),
        "decision_detail_labeled": 1,
        "label_version": LABEL_VERSION,
        "labeled_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def upsert_batch(conn, target_table, rows):
    if not rows:
        return 0
    sql = f"""
        INSERT INTO {quote_ident(target_table)} (
          source_comment_id, source_platform, source_table, source_record_key,
          external_id, comment_text, content_hash, intent_labels, decision_factor_primary,
          decision_factor_secondary, decision_result, decision_matched_keywords,
          decision_label_json, decision_detail_labeled, label_version, labeled_at
        ) VALUES (
          %(source_comment_id)s, %(source_platform)s, %(source_table)s, %(source_record_key)s,
          %(external_id)s, %(comment_text)s, %(content_hash)s, %(intent_labels)s, %(decision_factor_primary)s,
          %(decision_factor_secondary)s, %(decision_result)s, %(decision_matched_keywords)s,
          %(decision_label_json)s, %(decision_detail_labeled)s, %(label_version)s, %(labeled_at)s
        )
        ON DUPLICATE KEY UPDATE
          comment_text=VALUES(comment_text), content_hash=VALUES(content_hash),
          intent_labels=VALUES(intent_labels),
          decision_factor_primary=VALUES(decision_factor_primary),
          decision_factor_secondary=VALUES(decision_factor_secondary),
          decision_result=VALUES(decision_result),
          decision_matched_keywords=VALUES(decision_matched_keywords),
          decision_label_json=VALUES(decision_label_json),
          decision_detail_labeled=VALUES(decision_detail_labeled), labeled_at=VALUES(labeled_at)
    """
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
    return len(rows)


def run_database(source_table, target_table, *, all_rows=False, limit=0, dry_run=False):
    output_conn = connect_mysql()
    scanned = labeled = skipped_duplicate = 0
    results = {key: 0 for key in DECISION_RESULT_RULES}
    pending = []
    existing_hashes: set[str] = set()
    try:
        if not dry_run:
            ensure_target_table(output_conn, target_table)
            with output_conn.cursor() as cur:
                cur.execute(
                    f"SELECT DISTINCT content_hash FROM {quote_ident(target_table)} "
                    f"WHERE label_version = %s AND content_hash != ''",
                    (LABEL_VERSION,),
                )
                for r in cur.fetchall():
                    existing_hashes.add(r["content_hash"])
        for row in iter_source_rows(source_table, all_rows=all_rows, limit=limit):
            scanned += 1
            _text = normalize_text(row.get("comment_text"))
            _hash = hashlib.md5(_text.encode("utf-8")).hexdigest()
            if _hash in existing_hashes:
                skipped_duplicate += 1
                continue
            existing_hashes.add(_hash)
            output_row = build_db_row(row)
            labeled += 1
            results[output_row["decision_result"]] += 1
            if dry_run:
                continue
            pending.append(output_row)
            if len(pending) >= DB_BATCH_SIZE:
                upsert_batch(output_conn, target_table, pending)
                pending.clear()
        if not dry_run:
            upsert_batch(output_conn, target_table, pending)
    finally:
        output_conn.close()
    summary = {
        "mode": "database", "source_table": source_table,
        "target_table": target_table, "label_version": LABEL_VERSION,
        "all_rows": all_rows, "dry_run": dry_run,
        "scanned": scanned, "labeled": labeled, "decision_results": results,
        "skipped_duplicate": skipped_duplicate,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def run_file(args):
    input_path = Path(args.input)
    if not input_path.exists():
        raise FileNotFoundError(f"文件不存在：{input_path}")
    df = read_input_file(input_path)

    if args.text_col not in df.columns:
        raise ValueError(f"找不到评论字段：{args.text_col}")
    if not args.all_rows and args.intent_col not in df.columns:
        raise ValueError(f"找不到意图字段：{args.intent_col}")

    for col in [
        "decision_factor_primary", "decision_factor_secondary", "decision_result",
        "decision_matched_keywords", "decision_label_json", "decision_detail_labeled",
    ]:
        df[col] = ""

    for idx, row in df.iterrows():
        if not args.all_rows and not is_decision_row(row[args.intent_col]):
            continue
        text = normalize_text(row[args.text_col])
        if not text:
            continue
        result = label_decision_comment(text)
        df.at[idx, "decision_factor_primary"] = " | ".join(result["decision_factor_primary"])
        df.at[idx, "decision_factor_secondary"] = " | ".join(result["decision_factor_secondary"])
        df.at[idx, "decision_result"] = result["decision_result"]
        df.at[idx, "decision_matched_keywords"] = " | ".join(result["matched_keywords"])
        df.at[idx, "decision_label_json"] = json.dumps(result, ensure_ascii=False)
        df.at[idx, "decision_detail_labeled"] = True

    output_path = Path(args.output) if args.output else input_path.with_name(
        f"{input_path.stem}_decision_labeled.xlsx"
    )
    write_output_file(df, output_path)
    print(f"完成：{output_path}")
    print(f"共处理：{int((df['decision_detail_labeled'] == True).sum())} 条 Decision 评论")


def main():
    parser = argparse.ArgumentParser(
        description="Decision 评论：决策标准 + 决策结果打标"
    )

    parser.add_argument("input", nargs="?", help="输入 CSV / Excel 文件")
    parser.add_argument("-o", "--output", default=None)
    parser.add_argument("--text-col", default="comment")
    parser.add_argument("--intent-col", default="intent")
    parser.add_argument(
        "--all-rows",
        action="store_true",
        help="忽略 intent，对全部评论执行 Decision 细分类"
    )
    parser.add_argument("--database", action="store_true", help="从本地 MySQL 读取并写入结果表")
    parser.add_argument("--source-table", default=DEFAULT_SOURCE_TABLE)
    parser.add_argument("--target-table", default=DEFAULT_TARGET_TABLE)
    parser.add_argument("--limit", type=int, default=0, help="数据库模式最多处理行数，0 表示不限")
    parser.add_argument("--dry-run", action="store_true", help="数据库模式只统计，不建表或写入")

    args = parser.parse_args()

    if args.database:
        run_database(
            args.source_table, args.target_table,
            all_rows=args.all_rows, limit=args.limit, dry_run=args.dry_run,
        )
        return
    if not args.input:
        parser.error("文件模式需要 input，或使用 --database")
    run_file(args)


if __name__ == "__main__":
    main()
