#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
import pymysql


BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app_config import get_mysql_config  # noqa: E402


DEFAULT_SOURCE_TABLE = "catfood_choice_comments_filtered_v2"
DEFAULT_TARGET_TABLE = "catfood_need_comment_labels"
LABEL_VERSION = "need_comment_rules_v1"
DB_BATCH_SIZE = 1000

LIFE_STAGE_RULES = {
    "幼猫": [r"幼猫", r"奶猫", r"小奶猫", r"小猫", r"\d+\s*个?月(?:龄)?", r"半岁", r"未满一岁", r"不到一岁"],
    "成猫": [r"成猫", r"成年猫", r"成年了"],
    "老年猫": [r"老年猫", r"老猫", r"高龄猫", r"老龄猫", r"七岁以上", r"八岁", r"九岁", r"十岁"],
    "绝育阶段": [r"绝育后", r"刚绝育", r"准备绝育", r"绝育猫", r"绝育之后", r"做完绝育", r"做了绝育"],
}

BREED_RULES = {
    "英国短毛猫": [r"英国短毛猫", r"英短", r"蓝猫", r"英短蓝猫", r"金渐层", r"银渐层", r"金点", r"银点"],
    "美国短毛猫": [r"美国短毛猫", r"美短", r"美短虎斑"],
    "布偶猫": [r"布偶猫", r"布偶", r"海双", r"蓝双"],
    "缅因猫": [r"缅因猫", r"缅因"],
    "暹罗猫": [r"暹罗猫", r"暹罗"],
    "斯芬克斯猫": [r"斯芬克斯", r"无毛猫", r"加拿大无毛猫"],
    "中华田园猫": [r"中华田园猫", r"田园猫", r"狸花猫", r"狸花", r"橘猫", r"三花猫", r"奶牛猫"],
    "加菲猫": [r"加菲猫", r"加菲", r"异国短毛猫", r"异短"],
    "德文卷毛猫": [r"德文卷毛猫", r"德文猫", r"德文"],
    "阿比西尼亚猫": [r"阿比西尼亚", r"阿比"],
}

HEALTH_RULES = {
    "消化系统问题": {
        "软便/拉稀": [r"软便", r"拉稀", r"腹泻", r"稀便", r"便便不成形", r"不成形", r"水便", r"便便软"],
        "呕吐": [r"呕吐", r"吐粮", r"吐猫粮", r"吃完就吐", r"吐黄水", r"吐白沫", r"反胃"],
        "便秘": [r"便秘", r"拉不出来", r"排便困难", r"好几天不拉", r"不排便", r"大便干"],
        "便臭": [r"便臭", r"屎臭", r"便便臭", r"粑粑臭", r"臭便"],
        "消化不良": [r"消化不好", r"消化不良", r"不消化", r"肠胃不好", r"玻璃胃", r"肠胃敏感", r"肠胃脆弱"],
        "胀气/放屁": [r"胀气", r"放屁多", r"老放屁", r"屁多", r"肚子胀"],
    },
    "皮肤与毛发问题": {
        "黑下巴": [r"黑下巴", r"下巴黑", r"毛囊炎", r"下巴有黑点"],
        "掉毛": [r"掉毛", r"脱毛", r"疯狂掉毛", r"掉毛严重"],
        "毛发粗糙/毛质差": [r"毛发粗糙", r"毛糙", r"毛不亮", r"毛质差", r"毛发干枯"],
        "皮屑/皮肤干燥": [r"皮屑", r"皮肤干", r"皮肤干燥", r"起皮"],
        "皮脂分泌过多/油腻": [r"油腻", r"很油", r"太油", r"出油", r"油脂多", r"毛很油", r"油乎乎"],
        "瘙痒/皮肤敏感": [r"瘙痒", r"发痒", r"一直挠", r"总挠", r"皮肤敏感", r"皮肤红"],
    },
    "泌尿系统问题": {
        "尿闭/排尿困难": [r"尿闭", r"排尿困难", r"尿不出来", r"憋尿", r"蹲猫砂盆尿不出"],
        "尿频": [r"尿频", r"频繁尿", r"老上厕所", r"频繁进猫砂盆"],
        "结晶/结石": [r"尿结晶", r"结晶", r"尿结石", r"结石", r"膀胱结石"],
        "血尿": [r"血尿", r"尿血", r"尿里有血"],
        "泌尿敏感/泌尿管理": [r"泌尿敏感", r"泌尿问题", r"泌尿不好", r"泌尿粮", r"泌尿管理"],
    },
    "体重与代谢问题": {
        "易胖/体重增加": [r"易胖", r"容易胖", r"长胖", r"胖了", r"越来越胖", r"体重增加", r"发胖", r"绝育后胖"],
        "肥胖": [r"肥胖", r"超重", r"太胖", r"过胖"],
        "体重偏低/增重需求": [r"太瘦", r"偏瘦", r"不长肉", r"长不胖", r"需要增重", r"想增肥"],
        "饱腹感不足": [r"不顶饱", r"总是饿", r"老想吃", r"一直想吃", r"饱腹感", r"吃不饱"],
    },
    "过敏与敏感问题": {
        "食物敏感": [r"食物敏感", r"饮食敏感", r"敏感体质", r"吃很多粮都不适应"],
        "疑似食物过敏": [r"食物过敏", r"猫粮过敏", r"吃了过敏", r"疑似过敏", r"过敏体质"],
        "单一肉源/规避需求": [r"单一肉源", r"单一蛋白", r"低敏粮", r"低敏配方", r"不能吃鸡", r"鸡肉过敏", r"不能吃牛", r"不能吃鱼"],
    },
    "口腔问题": {
        "口臭": [r"口臭", r"嘴臭", r"嘴巴臭", r"口气重"],
        "牙结石/牙垢": [r"牙结石", r"牙垢", r"牙齿黄", r"牙齿脏"],
        "牙龈问题": [r"牙龈红", r"牙龈肿", r"牙龈炎", r"牙龈问题"],
    },
    "适口与进食问题": {
        "挑食": [r"挑食", r"嘴刁", r"很挑", r"挑嘴", r"挑粮"],
        "拒食/不爱吃": [r"不爱吃", r"拒食", r"闻了不吃", r"不肯吃", r"吃两口就走"],
        "食欲下降": [r"食欲下降", r"没食欲", r"食欲不好", r"吃得少", r"胃口不好"],
    },
}

def normalize_text(text):
    if text is None:
        return ""
    # NaN is the only common scalar value that is not equal to itself.
    try:
        if text != text:
            return ""
    except (TypeError, ValueError):
        pass
    return re.sub(r"\s+", " ", str(text)).strip()


def normalize_comment_time(raw, ref_dt=None):
    """将 source_comment_time 杂乱文本标准化为 'YYYY-MM-DD' 字符串，无法解析返回 None。

    支持的原始格式（来自 catfood_choice_comments_filtered_v2.source_comment_time）：
      - 'YYYY-MM-DD' / 'YYYY/MM/DD'（抖音主流，~51k 行）
      - 'MM-DD'（小红书无年份，~1.4k 行，用 ref_dt 推断年份）
      - 'N分钟前' / 'N小时前' / 'N天前' / 'N个月前'（相对时间，按 ref_dt 回推）
      - '昨天 HH:MM' / '前天' / '刚刚'
    ref_dt 为该行的 inserted_at（入库时间），是相对时间的唯一参照锚点。
    """
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None

    # 1) 完整日期 YYYY-MM-DD / YYYY/MM/DD（可能带时分秒，取前 10 位日期）
    m = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).strftime("%Y-%m-%d")
        except ValueError:
            return None

    ref = ref_dt or datetime.now()
    # 2) 昨天 / 前天
    if text.startswith("昨天"):
        return (ref - timedelta(days=1)).strftime("%Y-%m-%d")
    if text.startswith("前天"):
        return (ref - timedelta(days=2)).strftime("%Y-%m-%d")
    # 3) N分钟前
    m = re.match(r"^(\d+)\s*分钟前", text)
    if m:
        return (ref - timedelta(minutes=int(m.group(1)))).strftime("%Y-%m-%d")
    # 4) N小时前
    m = re.match(r"^(\d+)\s*小时前", text)
    if m:
        return (ref - timedelta(hours=int(m.group(1)))).strftime("%Y-%m-%d")
    # 5) N天前
    m = re.match(r"^(\d+)\s*天前", text)
    if m:
        return (ref - timedelta(days=int(m.group(1)))).strftime("%Y-%m-%d")
    # 6) N个月前 / N月前（按 30 天近似）
    m = re.match(r"^(\d+)\s*个?月前", text)
    if m:
        return (ref - timedelta(days=int(m.group(1)) * 30)).strftime("%Y-%m-%d")
    # 7) 刚刚
    if text.startswith("刚刚"):
        return ref.strftime("%Y-%m-%d")
    # 8) MM-DD 无年份，用 ref 的年份；若落在未来则回退一年
    m = re.match(r"^(\d{1,2})-(\d{1,2})$", text)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        try:
            cand = datetime(ref.year, mo, d)
        except ValueError:
            return None
        if cand.date() > ref.date():
            try:
                cand = datetime(ref.year - 1, mo, d)
            except ValueError:
                return None
        return cand.strftime("%Y-%m-%d")

    # 9) 兜底：从任意位置抽取 YYYY-MM-DD
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).strftime("%Y-%m-%d")
        except ValueError:
            return None
    return None


def unique(items):
    return list(dict.fromkeys(items))

def match_patterns(text, patterns):
    hits = []
    for pattern in patterns:
        for m in re.finditer(pattern, text, flags=re.I):
            hits.append(m.group(0))
    return unique(hits)

def detect_life_stage(text):
    labels, hits = [], []
    for label, patterns in LIFE_STAGE_RULES.items():
        found = match_patterns(text, patterns)
        if found:
            labels.append(label)
            hits += [f"生命阶段:{x}" for x in found]
    return unique(labels), unique(hits)

def detect_breed(text):
    labels, hits = [], []
    for label, patterns in BREED_RULES.items():
        found = match_patterns(text, patterns)
        if found:
            labels.append(label)
            hits += [f"品种:{x}" for x in found]
    return unique(labels), unique(hits)

def detect_health(text):
    primary, secondary, hits = [], [], []
    for p1, sub_map in HEALTH_RULES.items():
        p1_hit = False
        for p2, patterns in sub_map.items():
            found = match_patterns(text, patterns)
            if found:
                p1_hit = True
                secondary.append(f"{p1}>{p2}")
                hits += [f"健康:{p1}>{p2}:{x}" for x in found]
        if p1_hit:
            primary.append(p1)
    return unique(primary), unique(secondary), unique(hits)

def label_comment(text):
    text = normalize_text(text)
    life, life_hits = detect_life_stage(text)
    breed, breed_hits = detect_breed(text)
    hp, hs, health_hits = detect_health(text)
    return {
        "life_stage": life,
        "breed": breed,
        "health_primary": hp,
        "health_secondary": hs,
        "matched_keywords": unique(life_hits + breed_hits + health_hits),
    }

def is_need(value):
    value = normalize_text(value).lower()
    if not value:
        return False
    tokens = re.split(r"[,|;/、，；\s]+", value)
    return "need" in tokens or "需求" in value

def read_file(path):
    try:
        import pandas as pd
    except (ImportError, AttributeError) as exc:
        raise RuntimeError("文件模式需要安装完整 pandas：pip install pandas") from exc
    if not hasattr(pd, "read_csv"):
        raise RuntimeError("当前 pandas 安装不完整；文件模式请重新安装 pandas")
    suffix = path.suffix.lower()
    if suffix == ".csv":
        for enc in ("utf-8-sig", "utf-8", "gb18030"):
            try:
                return pd.read_csv(path, encoding=enc)
            except UnicodeDecodeError:
                pass
        raise ValueError("CSV 编码无法识别")
    if suffix in (".xlsx", ".xls"):
        return pd.read_excel(path)
    raise ValueError("仅支持 csv/xlsx/xls")

def write_file(df, path):
    if path.suffix.lower() == ".csv":
        df.to_csv(path, index=False, encoding="utf-8-sig")
    else:
        df.to_excel(path, index=False)


def quote_ident(name):
    if not re.fullmatch(r"[A-Za-z0-9_]+", name or ""):
        raise ValueError(f"不安全的表名: {name}")
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
              need_life_stage VARCHAR(255) NOT NULL DEFAULT '',
              need_breed VARCHAR(500) NOT NULL DEFAULT '',
              health_primary VARCHAR(500) NOT NULL DEFAULT '',
              health_secondary TEXT NOT NULL,
              need_matched_keywords TEXT NOT NULL,
              need_label_json JSON NOT NULL,
              need_detail_labeled TINYINT(1) NOT NULL DEFAULT 0,
              content_hash VARCHAR(32) NOT NULL DEFAULT '',
              label_version VARCHAR(64) NOT NULL,
              labeled_at DATETIME NOT NULL,
              source_comment_time DATE NULL,
              UNIQUE KEY uq_source_label (source_comment_id, label_version),
              KEY idx_source_record (source_platform, source_table, source_record_key),
              KEY idx_need_detail (need_detail_labeled),
              KEY idx_label_version (label_version),
              KEY idx_source_comment_time (source_comment_time)
            ) DEFAULT CHARSET=utf8mb4
            """
        )
        # 兼容已存在的旧表：补字段（忽略已存在时报错，errno=1060）
        for col_def in (
            "ADD COLUMN source_comment_time DATE NULL AFTER labeled_at",
            "ADD COLUMN content_hash VARCHAR(32) NOT NULL DEFAULT ''",
        ):
            try:
                cur.execute(f"ALTER TABLE {quote_ident(target_table)} {col_def}")
            except pymysql.err.OperationalError as exc:
                if exc.args and exc.args[0] != 1060:
                    raise
        # 补索引（忽略已存在时报错，errno=1061）
        for idx_def in (
            "ADD KEY idx_source_comment_time (source_comment_time)",
            "ADD KEY idx_content_hash (content_hash)",
        ):
            try:
                cur.execute(f"ALTER TABLE {quote_ident(target_table)} {idx_def}")
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
                   external_id, comment_text, intent_labels,
                   source_comment_time, inserted_at
            FROM {quote_ident(source_table)}
            WHERE comment_text IS NOT NULL AND TRIM(comment_text) <> ''
        """
        params = []
        if not all_rows:
            sql += " AND FIND_IN_SET('Need', REPLACE(intent_labels, '、', ',')) > 0"
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
    result = label_comment(row.get("comment_text"))
    return {
        "source_comment_id": row.get("id"),
        "source_platform": normalize_text(row.get("source_platform")),
        "source_table": normalize_text(row.get("source_table")),
        "source_record_key": normalize_text(row.get("source_record_key")),
        "external_id": normalize_text(row.get("external_id")) or None,
        "comment_text": normalize_text(row.get("comment_text")),
        "content_hash": hashlib.md5(normalize_text(row.get("comment_text")).encode("utf-8")).hexdigest(),
        "intent_labels": normalize_text(row.get("intent_labels")),
        "need_life_stage": " | ".join(result["life_stage"]),
        "need_breed": " | ".join(result["breed"]),
        "health_primary": " | ".join(result["health_primary"]),
        "health_secondary": " | ".join(result["health_secondary"]),
        "need_matched_keywords": " | ".join(result["matched_keywords"]),
        "need_label_json": json.dumps(result, ensure_ascii=False),
        "need_detail_labeled": int(
            bool(result["life_stage"] or result["breed"] or result["health_secondary"])
        ),
        "label_version": LABEL_VERSION,
        "labeled_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_comment_time": normalize_comment_time(
            row.get("source_comment_time"), row.get("inserted_at")
        ),
    }


def upsert_batch(conn, target_table, rows):
    if not rows:
        return 0
    sql = f"""
        INSERT INTO {quote_ident(target_table)} (
          source_comment_id, source_platform, source_table, source_record_key,
          external_id, comment_text, content_hash, intent_labels, need_life_stage, need_breed,
          health_primary, health_secondary, need_matched_keywords, need_label_json,
          need_detail_labeled, label_version, labeled_at, source_comment_time
        ) VALUES (
          %(source_comment_id)s, %(source_platform)s, %(source_table)s, %(source_record_key)s,
          %(external_id)s, %(comment_text)s, %(content_hash)s, %(intent_labels)s, %(need_life_stage)s, %(need_breed)s,
          %(health_primary)s, %(health_secondary)s, %(need_matched_keywords)s, %(need_label_json)s,
          %(need_detail_labeled)s, %(label_version)s, %(labeled_at)s, %(source_comment_time)s
        )
        ON DUPLICATE KEY UPDATE
          comment_text=VALUES(comment_text), intent_labels=VALUES(intent_labels),
          need_life_stage=VALUES(need_life_stage), need_breed=VALUES(need_breed),
          health_primary=VALUES(health_primary), health_secondary=VALUES(health_secondary),
          need_matched_keywords=VALUES(need_matched_keywords),
          need_label_json=VALUES(need_label_json),
          need_detail_labeled=VALUES(need_detail_labeled), labeled_at=VALUES(labeled_at),
          source_comment_time=VALUES(source_comment_time)
    """
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
    return len(rows)


def run_database(source_table, target_table, *, all_rows=False, limit=0, dry_run=False):
    output_conn = connect_mysql()
    scanned = labeled = detailed = skipped_duplicate = 0
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
            detailed += output_row["need_detail_labeled"]
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
        "mode": "database",
        "source_table": source_table,
        "target_table": target_table,
        "label_version": LABEL_VERSION,
        "all_rows": all_rows,
        "dry_run": dry_run,
        "scanned": scanned,
        "labeled": labeled,
        "need_detail_labeled": detailed,
        "skipped_duplicate": skipped_duplicate,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def run_file(args):
    input_path = Path(args.input)
    df = read_file(input_path)

    if args.text_col not in df.columns:
        raise ValueError(f"评论字段不存在: {args.text_col}")
    if not args.all_rows and args.intent_col not in df.columns:
        raise ValueError(f"意图字段不存在: {args.intent_col}")

    for col in [
        "need_life_stage", "need_breed", "health_primary",
        "health_secondary", "need_matched_keywords",
        "need_label_json", "need_detail_labeled"
    ]:
        df[col] = ""

    for idx, row in df.iterrows():
        if not args.all_rows and not is_need(row[args.intent_col]):
            continue

        result = label_comment(row[args.text_col])
        df.at[idx, "need_life_stage"] = " | ".join(result["life_stage"])
        df.at[idx, "need_breed"] = " | ".join(result["breed"])
        df.at[idx, "health_primary"] = " | ".join(result["health_primary"])
        df.at[idx, "health_secondary"] = " | ".join(result["health_secondary"])
        df.at[idx, "need_matched_keywords"] = " | ".join(result["matched_keywords"])
        df.at[idx, "need_label_json"] = json.dumps(result, ensure_ascii=False)
        df.at[idx, "need_detail_labeled"] = bool(
            result["life_stage"] or result["breed"] or result["health_secondary"]
        )

    output_path = Path(args.output) if args.output else input_path.with_name(
        input_path.stem + "_need_labeled.xlsx"
    )
    write_file(df, output_path)
    print(f"完成: {output_path}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", nargs="?", help="文件模式输入路径")
    parser.add_argument("-o", "--output", default=None)
    parser.add_argument("--text-col", default="comment")
    parser.add_argument("--intent-col", default="intent")
    parser.add_argument("--all-rows", action="store_true", help="忽略 intent，对所有评论打标")
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
