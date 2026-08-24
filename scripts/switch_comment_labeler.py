#!/usr/bin/env python3
# -*- coding: utf-8 -*-

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
DEFAULT_TARGET_TABLE = "catfood_switch_comment_labels"
LABEL_VERSION = "switch_comment_rules_v1"
DB_BATCH_SIZE = 1000

# DB 不可用时的品牌别名回退词典；运行时由 load_brand_aliases() 覆盖为从
# catfood_standard_brand + _alias 表加载的完整品牌别名表（~124 品牌 + ~218 别名）。
FALLBACK_BRAND_ALIASES = {
    "皇家": [re.escape("皇家")],
    "渴望": [re.escape("渴望")],
    "巅峰": [re.escape("巅峰"), re.escape("ZIWI")],
    "麦富迪": [re.escape("麦富迪")],
    "网易严选": [re.escape("网易严选")],
}
_BRAND_ALIASES_CACHE: dict | None = None

SWITCH_REASON_RULES = {
    "健康/适配原因": {
        "软便/拉稀": [r"软便", r"拉稀", r"腹泻", r"稀便", r"便便不成形", r"水便"],
        "呕吐": [r"呕吐", r"吐粮", r"吐猫粮", r"吃完就吐", r"吐黄水", r"吐白沫"],
        "便秘": [r"便秘", r"拉不出来", r"排便困难"],
        "便臭": [r"便臭", r"屎臭", r"便便臭", r"粑粑臭"],
        "黑下巴": [r"黑下巴", r"下巴黑", r"毛囊炎"],
        "掉毛/毛发问题": [r"掉毛", r"脱毛", r"毛糙", r"皮屑"],
        "泌尿问题": [r"泌尿", r"尿闭", r"尿频", r"结晶", r"结石", r"血尿"],
        "体重问题": [r"易胖", r"肥胖", r"太胖", r"体重增加", r"绝育后胖"],
        "过敏/敏感": [r"过敏", r"食物敏感", r"敏感体质", r"不能吃鸡", r"鸡肉过敏"],
        "挑食/拒食": [r"挑食", r"挑嘴", r"不爱吃", r"拒食", r"不肯吃"],
    },
    "产品体验原因": {
        "油腻/油脂高": [r"太油", r"很油", r"油腻", r"出油", r"喷油", r"油乎乎"],
        "适口性差": [r"适口性差", r"不爱吃", r"不吃", r"拒食", r"挑食"],
        "粉多/碎粮": [r"粉多", r"粉末多", r"碎粮", r"太碎", r"渣多"],
        "颗粒问题": [r"颗粒大", r"颗粒小", r"颗粒硬", r"太硬"],
        "气味问题": [r"味道重", r"气味大", r"太臭", r"味道太大"],
        "配方结构": [r"配方复杂", r"配方太杂", r"肉源太多", r"原料太多", r"多肉源"],
        "配方调整/版本变化": [r"换配方", r"配方变了", r"新版", r"旧版", r"改配方"],
        "产品稳定性": [r"批次不稳定", r"批次问题", r"稳定性差", r"一批一个样"],
    },
    "价格原因": {
        "价格上涨": [r"涨价", r"价格涨了", r"越来越贵", r"涨了好多"],
        "价格高": [r"太贵", r"贵了", r"价格高", r"有点贵", r"吃不起", r"买不起"],
        "性价比": [r"性价比", r"不划算", r"值不值", r"同价位", r"不值这个价"],
        "长期成本": [r"长期吃不起", r"长期成本", r"长期喂", r"一个月太贵"],
        "活动减少": [r"活动少", r"没活动", r"优惠少", r"券少", r"促销少"],
    },
    "品牌信任原因": {
        "品控问题": [r"品控", r"质量问题", r"批次问题", r"翻车", r"出过问题"],
        "负面口碑": [r"差评", r"负面", r"口碑不好", r"评价不好"],
        "安全担忧": [r"安全问题", r"不敢喂", r"不放心", r"担心安全", r"召回"],
        "工厂/生产信任": [r"代工厂", r"工厂", r"生产商", r"换工厂", r"生产方"],
        "原料来源": [r"原料来源", r"原料不透明", r"供应商", r"溯源", r"原料不放心"],
        "品牌信任下降": [r"不信任", r"不敢买", r"失望", r"品牌不靠谱", r"以后不买"],
    },
    "主动升级/尝试": {
        "生命阶段变化": [r"幼猫换成猫", r"成年了", r"老年了", r"绝育后", r"刚绝育", r"年龄大了"],
        "功能升级": [r"想换肠胃粮", r"想换泌尿粮", r"想换低敏", r"想换体重管理", r"功能粮"],
        "尝试新品": [r"想试试", r"换着吃", r"尝鲜", r"试试看", r"换个口味"],
        "追求更优方案": [r"想换更好的", r"想升级", r"想找更适合", r"想找更稳定", r"换个更好的"],
    },
}

SWITCH_STATUS_RULES = {
    "迁移后回退": [r"又换回", r"换回原来", r"最后又换回", r"还是换回", r"换回.*了"],
    "已迁移": [r"换成了", r"换到了", r"已经换", r"后来换", r"改吃", r"现在换成", r"从.*换到", r"从.*换成", r"现在吃.*了"],
    "准备迁移": [r"准备换", r"打算换", r"想换", r"考虑换", r"计划换", r"准备从", r"打算从", r"想从"],
}

# "未明确" 不在 SWITCH_STATUS_RULES 中，作为 detect_switch_status 的默认返回值，
# 与显式命中"准备迁移"区分，便于识别上游 Switch 标签可能存在的误标。
STATUS_PRIORITY = {"迁移后回退": 3, "已迁移": 2, "准备迁移": 1, "未明确": 0}

def normalize_text(text):
    if text is None:
        return ""
    try:
        if text != text:
            return ""
    except (TypeError, ValueError):
        pass
    return re.sub(r"\s+", " ", str(text).replace("\u3000", " ")).strip()

def unique(items):
    return list(dict.fromkeys(items))

def regex_matches(text, patterns):
    hits = []
    for p in patterns:
        for m in re.finditer(p, text, flags=re.I):
            if m.group(0).strip():
                hits.append(m.group(0).strip())
    return unique(hits)

def load_brand_aliases():
    """Load active standard brands and aliases from the local brand master."""
    global _BRAND_ALIASES_CACHE
    if _BRAND_ALIASES_CACHE is not None:
        return _BRAND_ALIASES_CACHE
    aliases = {}
    try:
        conn = connect_mysql()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT b.standard_brand_name, a.alias_name
                    FROM catfood_standard_brand b
                    LEFT JOIN catfood_standard_brand_alias a
                      ON a.brand_id = b.brand_id AND a.active = 1
                    WHERE b.active = 1
                    """
                )
                for row in cur.fetchall():
                    standard = normalize_text(row.get("standard_brand_name"))
                    alias = normalize_text(row.get("alias_name"))
                    if not standard:
                        continue
                    names = aliases.setdefault(standard, set())
                    names.add(standard)
                    if alias:
                        names.add(alias)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        print(f"[brand aliases] 数据库加载失败，使用内置回退词典：{exc}", file=sys.stderr)
        _BRAND_ALIASES_CACHE = FALLBACK_BRAND_ALIASES
        return _BRAND_ALIASES_CACHE

    _BRAND_ALIASES_CACHE = {
        standard: [re.escape(name) for name in sorted(names, key=len, reverse=True)]
        for standard, names in aliases.items()
    } or FALLBACK_BRAND_ALIASES
    return _BRAND_ALIASES_CACHE


def detect_brand_mentions(text, alias_map=None):
    """Return (position, standard brand, matched alias), ordered by occurrence."""
    alias_map = alias_map or load_brand_aliases()
    mentions = []
    for standard, patterns in alias_map.items():
        best = None
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.I)
            if match and (best is None or match.start() < best.start()):
                best = match
        if best:
            mentions.append((best.start(), standard, best.group(0)))
    return sorted(mentions, key=lambda item: item[0])


def detect_known_entities(text, alias_map=None):
    return unique(standard for _, standard, _ in detect_brand_mentions(text, alias_map))

def detect_switch_path(text):
    alias_map = load_brand_aliases()
    mentions = detect_brand_mentions(text, alias_map)
    brands = unique(standard for _, standard, _ in mentions)
    from_brand = ""
    to_brand = ""
    evidence = []

    # 句式 A：从/之前吃X...换到/换成/换回Y（间隔放宽到 30 字符，覆盖换回语义）
    # 句式 B：X换/换成/换到/换回Y（from/to 至少一方需为已知品牌，避免"今天换衣服"类无意义匹配）
    patterns = [
        re.compile(
            r"(?:从|之前吃|原来吃|以前吃|一直吃)\s*"
            r"(?P<from>[^，。！？；,\s]{1,20})"
            r".{0,30}?"
            r"(?:换到|换成|改成|改吃|换回|后来吃|现在吃)\s*"
            r"(?P<to>[^，。！？；,\s]{1,20})"
        ),
        re.compile(
            r"(?P<from>[^，。！？；,\s]{1,20})\s*"
            r"(?:换|换成|换到|换回)\s*"
            r"(?P<to>[^，。！？；,\s]{1,20})"
        ),
    ]

    def norm(fragment):
        fragment_mentions = detect_brand_mentions(fragment, alias_map)
        if fragment_mentions:
            return fragment_mentions[0][1]
        return ""

    # 用 finditer 遍历所有匹配，取最后一个有效的（代表最终迁移路径）
    for p in patterns:
        last_valid = None
        for m in p.finditer(text):
            fb = norm(m.group("from"))
            tb = norm(m.group("to"))
            # from/to 至少一方需为已知品牌，过滤"今天换衣服"等无意义匹配
            if not fb and not tb:
                continue
            last_valid = (m, fb, tb)
        if last_valid:
            m, fb, tb = last_valid
            from_brand = fb
            to_brand = tb
            evidence.append("路径句式:" + m.group(0))
            break

    if (not from_brand or not to_brand) and len(brands) >= 2 and re.search(r"换|改吃|后来|现在吃", text):
        from_brand = from_brand or brands[0]
        to_brand = to_brand or brands[1]
        evidence.append("路径推断:品牌出现顺序")

    return from_brand, to_brand, unique(evidence)

def detect_switch_reason(text):
    p1s, p2s, hits = [], [], []
    for p1, sub_map in SWITCH_REASON_RULES.items():
        hit_primary = False
        for p2, patterns in sub_map.items():
            found = regex_matches(text, patterns)
            if found:
                hit_primary = True
                p2s.append(f"{p1}>{p2}")
                hits += [f"原因:{p1}>{p2}:{x}" for x in found]
        if hit_primary:
            p1s.append(p1)
    return unique(p1s), unique(p2s), unique(hits)

def detect_switch_status(text):
    candidates, hits = [], []
    for status, patterns in SWITCH_STATUS_RULES.items():
        found = regex_matches(text, patterns)
        if found:
            candidates.append(status)
            hits += [f"状态:{status}:{x}" for x in found]
    if not candidates:
        return "未明确", ["状态:未明确:默认"]
    selected = max(candidates, key=lambda x: STATUS_PRIORITY[x])
    return selected, unique(hits)

def label_switch_comment(text):
    text = normalize_text(text)
    from_brand, to_brand, path_hits = detect_switch_path(text)
    rp, rs, reason_hits = detect_switch_reason(text)
    status, status_hits = detect_switch_status(text)

    return {
        "from_brand": from_brand,
        "to_brand": to_brand,
        "switch_reason_primary": rp,
        "switch_reason_secondary": rs,
        "switch_status": status,
        "matched_keywords": unique(path_hits + reason_hits + status_hits),
    }

def is_switch_row(value):
    value = normalize_text(value)
    if not value:
        return False
    lower = value.lower()
    tokens = re.split(r"[,|;/、，；\s]+", lower)
    return "switch" in tokens or "swich" in tokens or "迁移" in value or "换粮" in value

def read_file(path):
    try:
        import pandas as pd
    except (ImportError, AttributeError) as exc:
        raise RuntimeError("文件模式需要安装完整 pandas：pip install pandas") from exc
    if not hasattr(pd, "read_csv"):
        raise RuntimeError("当前 pandas 安装不完整；文件模式请重新安装 pandas")
    if path.suffix.lower() == ".csv":
        for enc in ("utf-8-sig", "utf-8", "gb18030"):
            try:
                return pd.read_csv(path, encoding=enc)
            except UnicodeDecodeError:
                pass
        raise ValueError("CSV 编码无法识别")
    if path.suffix.lower() in (".xlsx", ".xls"):
        return pd.read_excel(path)
    raise ValueError("仅支持 CSV / XLSX / XLS")

def write_file(df, path):
    if path.suffix.lower() == ".csv":
        df.to_csv(path, index=False, encoding="utf-8-sig")
    else:
        df.to_excel(path, index=False)


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
              switch_from_brand VARCHAR(255) NOT NULL DEFAULT '',
              switch_to_brand VARCHAR(255) NOT NULL DEFAULT '',
              switch_reason_primary VARCHAR(500) NOT NULL DEFAULT '',
              switch_reason_secondary TEXT NOT NULL,
              switch_status VARCHAR(32) NOT NULL,
              switch_matched_keywords TEXT NOT NULL,
              switch_label_json JSON NOT NULL,
              switch_detail_labeled TINYINT(1) NOT NULL DEFAULT 0,
              content_hash VARCHAR(32) NOT NULL DEFAULT '',
              label_version VARCHAR(64) NOT NULL,
              labeled_at DATETIME NOT NULL,
              UNIQUE KEY uq_source_label (source_comment_id, label_version),
              KEY idx_source_record (source_platform, source_table, source_record_key),
              KEY idx_switch_path (switch_from_brand, switch_to_brand),
              KEY idx_switch_status (switch_status),
              KEY idx_switch_detail (switch_detail_labeled),
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
            sql += " AND FIND_IN_SET('Switch', REPLACE(intent_labels, '、', ',')) > 0"
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
    result = label_switch_comment(row.get("comment_text"))
    return {
        "source_comment_id": row.get("id"),
        "source_platform": normalize_text(row.get("source_platform")),
        "source_table": normalize_text(row.get("source_table")),
        "source_record_key": normalize_text(row.get("source_record_key")),
        "external_id": normalize_text(row.get("external_id")) or None,
        "comment_text": normalize_text(row.get("comment_text")),
        "content_hash": hashlib.md5(normalize_text(row.get("comment_text")).encode("utf-8")).hexdigest(),
        "intent_labels": normalize_text(row.get("intent_labels")),
        "switch_from_brand": result["from_brand"],
        "switch_to_brand": result["to_brand"],
        "switch_reason_primary": " | ".join(result["switch_reason_primary"]),
        "switch_reason_secondary": " | ".join(result["switch_reason_secondary"]),
        "switch_status": result["switch_status"],
        "switch_matched_keywords": " | ".join(result["matched_keywords"]),
        "switch_label_json": json.dumps(result, ensure_ascii=False),
        "switch_detail_labeled": int(bool(
            result["from_brand"] or result["to_brand"] or result["switch_reason_primary"]
        )),
        "label_version": LABEL_VERSION,
        "labeled_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def upsert_batch(conn, target_table, rows):
    if not rows:
        return 0
    sql = f"""
        INSERT INTO {quote_ident(target_table)} (
          source_comment_id, source_platform, source_table, source_record_key,
          external_id, comment_text, content_hash, intent_labels, switch_from_brand, switch_to_brand,
          switch_reason_primary, switch_reason_secondary, switch_status,
          switch_matched_keywords, switch_label_json, switch_detail_labeled,
          label_version, labeled_at
        ) VALUES (
          %(source_comment_id)s, %(source_platform)s, %(source_table)s, %(source_record_key)s,
          %(external_id)s, %(comment_text)s, %(content_hash)s, %(intent_labels)s, %(switch_from_brand)s, %(switch_to_brand)s,
          %(switch_reason_primary)s, %(switch_reason_secondary)s, %(switch_status)s,
          %(switch_matched_keywords)s, %(switch_label_json)s, %(switch_detail_labeled)s,
          %(label_version)s, %(labeled_at)s
        )
        ON DUPLICATE KEY UPDATE
          comment_text=VALUES(comment_text), content_hash=VALUES(content_hash),
          intent_labels=VALUES(intent_labels),
          switch_from_brand=VALUES(switch_from_brand), switch_to_brand=VALUES(switch_to_brand),
          switch_reason_primary=VALUES(switch_reason_primary),
          switch_reason_secondary=VALUES(switch_reason_secondary),
          switch_status=VALUES(switch_status),
          switch_matched_keywords=VALUES(switch_matched_keywords),
          switch_label_json=VALUES(switch_label_json),
          switch_detail_labeled=VALUES(switch_detail_labeled), labeled_at=VALUES(labeled_at)
    """
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
    return len(rows)


def run_database(source_table, target_table, *, all_rows=False, limit=0, dry_run=False):
    output_conn = connect_mysql()
    scanned = labeled = complete_paths = reason_hits = skipped_duplicate = 0
    # statuses 需包含"未明确"作为默认值，与显式"准备迁移"区分
    statuses = {key: 0 for key in list(SWITCH_STATUS_RULES.keys()) + ["未明确"]}
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
            statuses[output_row["switch_status"]] = statuses.get(output_row["switch_status"], 0) + 1
            complete_paths += int(bool(output_row["switch_from_brand"] and output_row["switch_to_brand"]))
            reason_hits += int(bool(output_row["switch_reason_primary"]))
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
        "scanned": scanned, "labeled": labeled,
        "complete_brand_paths": complete_paths,
        "reason_hits": reason_hits,
        "switch_statuses": statuses,
        "skipped_duplicate": skipped_duplicate,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def run_file(args):
    input_path = Path(args.input)
    if not input_path.exists():
        raise FileNotFoundError(f"文件不存在：{input_path}")
    df = read_file(input_path)
    if args.text_col not in df.columns:
        raise ValueError(f"找不到评论字段：{args.text_col}")
    if not args.all_rows and args.intent_col not in df.columns:
        raise ValueError(f"找不到意图字段：{args.intent_col}")
    cols = [
        "switch_from_brand", "switch_to_brand", "switch_reason_primary",
        "switch_reason_secondary", "switch_status", "switch_matched_keywords",
        "switch_label_json", "switch_detail_labeled",
    ]
    for col in cols:
        df[col] = ""
    for idx, row in df.iterrows():
        if not args.all_rows and not is_switch_row(row[args.intent_col]):
            continue
        result = label_switch_comment(row[args.text_col])
        df.at[idx, "switch_from_brand"] = result["from_brand"]
        df.at[idx, "switch_to_brand"] = result["to_brand"]
        df.at[idx, "switch_reason_primary"] = " | ".join(result["switch_reason_primary"])
        df.at[idx, "switch_reason_secondary"] = " | ".join(result["switch_reason_secondary"])
        df.at[idx, "switch_status"] = result["switch_status"]
        df.at[idx, "switch_matched_keywords"] = " | ".join(result["matched_keywords"])
        df.at[idx, "switch_label_json"] = json.dumps(result, ensure_ascii=False)
        df.at[idx, "switch_detail_labeled"] = True
    output_path = Path(args.output) if args.output else input_path.with_name(input_path.stem + "_switch_labeled.xlsx")
    write_file(df, output_path)
    print(f"完成：{output_path}")
    print(f"共处理：{int((df['switch_detail_labeled'] == True).sum())} 条 Switch 评论")

def main():
    parser = argparse.ArgumentParser(description="Switch 评论结构化打标")
    parser.add_argument("input", nargs="?", help="输入 CSV / Excel 文件")
    parser.add_argument("-o", "--output", default=None)
    parser.add_argument("--text-col", default="comment")
    parser.add_argument("--intent-col", default="intent")
    parser.add_argument("--all-rows", action="store_true")
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
