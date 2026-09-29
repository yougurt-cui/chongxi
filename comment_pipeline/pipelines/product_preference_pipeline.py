"""Extract product preference events with LLM and deterministic fallback."""

from __future__ import annotations

import json
import re
from typing import Any

from comment_pipeline.common.cleaner import is_low_information, strip_platform_noise
from comment_pipeline.common.llm_client import JsonLlmClient


PREFERENCE_SYSTEM_PROMPT = """
你是宠物产品偏好信息抽取器。只提取评论明确表达的信息，不推测品牌或属性。
输出严格 JSON，字段：material 字符串数组、shape 字符串、size 字符串、function 字符串数组、
structure 字符串数组、interaction 字符串数组、benefit 字符串数组、
user_proof 字符串数组、pain_point 字符串数组、pet_action 字符串数组、
preference（positive/negative/mixed/neutral）、reason 字符串数组、durability 字符串、
evidence_text 字符串、confidence 0到1。evidence_text必须来自输入评论原文。
structure只写可观察的产品结构或反馈机制；interaction只写宠物与产品的具体动作；
benefit只写评论明确体现的使用收益；user_proof写能够证明真实使用强度或持续性的事实；
pain_point写用户明确抱怨的问题。每个数组项使用简短、可聚合的中文短语，不得重复原句或推测。
""".strip()

MATERIAL_WORDS = ("毛绒", "塑料", "硅胶", "木质", "纸板", "瓦楞纸", "金属", "橡胶", "陶瓷")
FUNCTION_WORDS = ("自动", "漏食", "发射零食", "磨牙", "抓挠", "饮水", "投食", "互动", "发声", "滚动")
ACTION_WORDS = ("抱", "后腿蹬", "蹬", "追", "扑", "咬", "抓", "舔", "闻", "吃", "玩", "钻")
POSITIVE_RE = re.compile(r"(?<!不)喜欢|(?<!不)爱玩|(?<!不)愿意|(?<!不)爱吃|接受|(?<![没不])感兴趣|主动|上头|不错|好用|推荐|最夯")
NEGATIVE_RE = re.compile(r"不喜欢|不玩|不愿意|不吃|不感兴趣|排斥|没兴趣|闻了就走|难用|难闻|太吵|声音大|麻烦|鸡肋|退货")
DURABILITY_BAD_RE = re.compile(r"不耐用|容易坏|坏了|开裂|断了|线头|玩具掉毛|毛绒掉毛|破了|玩烂|玩.{0,6}(?:两天|几天).{0,6}(?:坏|开线|线头|破)")
DURABILITY_GOOD_RE = re.compile(r"耐用|结实|牢固|用了很久|玩不坏")

STRUCTURE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"自动.{0,6}(?:移动|滚动|跑|球)|电动.{0,6}球", "自动移动结构"),
    (r"不倒翁|摇摆", "不倒翁摇摆结构"),
    (r"绳子|绳状|鞋带|腰带|拉绳", "绳状附件"),
    (r"羽毛|小鸟|鸟类|老鼠|蝴蝶", "鸟类/老鼠类挂件"),
    (r"叽叽|沙沙|铃铛|发声|声音反馈", "叽叽声/沙沙声反馈"),
    (r"轨道.{0,5}球|球.{0,5}轨道", "轨道球结构"),
    (r"漏食|出粮口|零食.{0,5}(?:投放|发射)", "漏食/投食结构"),
)
INTERACTION_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"叼着.{0,8}(?:跑|走)|叼走", "叼着移动"),
    (r"弹走.{0,8}追|追.{0,8}(?:球|玩具)", "弹走后再次追回"),
    (r"扒拉|爪.{0,5}(?:拨|拍|抓)", "用爪子扒拉"),
    (r"拖着.{0,6}绳|拖绳", "拖着绳子玩"),
    (r"(?:停机|停止|关闭|停了).{0,16}(?:继续|还要|还在).{0,10}(?:玩|扒拉|咬)", "停机后继续玩附件"),
    (r"抱着.{0,5}蹬|后腿蹬|踢踢", "抱住后腿蹬"),
    (r"钻.{0,5}(?:隧道|洞)|隧道.{0,5}钻", "钻入穿行"),
)
BENEFIT_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"消耗精力|消耗体力|放电|玩累|累趴", "消耗精力"),
    (r"(?:减少|不再|很少).{0,8}打架|打架.{0,8}(?:少了|减少)", "减少宠物打架"),
    (r"自己玩|自主玩|独自玩|一个人玩|玩很久", "提高自主玩耍时间"),
    (r"不用陪玩|减少陪玩|解放双手|主人.{0,6}省心", "减少主人陪玩需求"),
    (r"缓解.{0,5}无聊|不无聊", "缓解无聊"),
)
USER_PROOF_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"玩烂了.{0,5}(?:两个|几个|好几个)|玩坏了.{0,5}(?:两个|几个|好几个)", "重复玩坏多个"),
    (r"每天.{0,5}玩|天天.{0,5}玩", "每天都玩"),
    (r"(?:停机|停止|关闭|停了).{0,15}(?:打开|开起来|继续开)", "停机后要求再次打开"),
    (r"睡觉前.{0,12}(?:藏|收起来)|晚上.{0,8}(?:藏|收玩具)", "睡前需要收起玩具"),
    (r"两只猫.{0,8}(?:都喜欢|都爱玩)|俩猫.{0,8}(?:都喜欢|都爱玩)", "多只宠物都喜欢"),
    (r"玩了.{0,5}(?:几个月|半年|一年)|用了.{0,5}(?:几个月|半年|一年)", "持续使用数月"),
)
PAIN_POINT_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"声音.{0,4}(?:大|吵)|太吵|噪音", "运行声音较大"),
    (r"不耐用|容易坏|质量不好|开裂|断了|线头|破了|玩烂", "耐用性不足"),
    (r"塑料味|难闻|异味|味道大", "气味明显"),
    (r"太大|尺寸大|占地方", "尺寸过大/占空间"),
    (r"太小|尺寸小", "尺寸偏小"),
    (r"难清理|不好洗|清洁麻烦", "清洁不便"),
    (r"容易卡住|卡住不动", "容易卡住"),
)


def _unique_matches(words: tuple[str, ...], text: str) -> list[str]:
    return list(dict.fromkeys(word for word in words if word in text))


def _normalized_matches(patterns: tuple[tuple[str, str], ...], text: str) -> list[str]:
    return list(dict.fromkeys(label for pattern, label in patterns if re.search(pattern, text)))


def extract_text(comment: Any) -> str:
    """抽取用的正文：优先使用清洗后的文本，并对历史脏数据再兜一层。"""
    text = strip_platform_noise(str(getattr(comment, "clean_text", "") or "")).strip()
    return re.sub(r"\s+", " ", text)


def fallback_extract(comment: Any) -> dict[str, Any]:
    text = extract_text(comment)
    positive, negative = bool(POSITIVE_RE.search(text)), bool(NEGATIVE_RE.search(text))
    preference = "mixed" if positive and negative else "positive" if positive else "negative" if negative else "neutral"
    durability = "较差" if DURABILITY_BAD_RE.search(text) else "较好" if DURABILITY_GOOD_RE.search(text) else ""
    reasons: list[str] = []
    if positive:
        reasons.append("宠物主动接受或互动")
    if negative:
        reasons.append("宠物拒绝或用户负面评价")
    if durability == "较差":
        reasons.append("出现损坏或做工问题")
    return {
        "material": _unique_matches(MATERIAL_WORDS, text),
        "shape": next((word for word in ("小鱼", "球形", "圆形", "隧道", "棒状") if word in text), ""),
        "size": next((word for word in ("太大", "太小", "偏大", "偏小", "大小合适") if word in text), ""),
        "function": _unique_matches(FUNCTION_WORDS, text),
        "structure": _normalized_matches(STRUCTURE_PATTERNS, text),
        "interaction": _normalized_matches(INTERACTION_PATTERNS, text),
        "benefit": _normalized_matches(BENEFIT_PATTERNS, text),
        "user_proof": _normalized_matches(USER_PROOF_PATTERNS, text),
        "pain_point": _normalized_matches(PAIN_POINT_PATTERNS, text),
        "pet_action": _unique_matches(ACTION_WORDS, text),
        "preference": preference,
        "reason": reasons,
        "durability": durability,
        "evidence_text": text,
        "confidence": 0.72 if preference != "neutral" else 0.55,
        "extraction_source": "rule",
    }


def extract_preference(comment: Any, llm_client: JsonLlmClient | None = None) -> dict[str, Any] | None:
    """抽取偏好事件；正文清洗后信息量不足则返回 None，由调用方跳过落库。"""
    text = extract_text(comment)
    if is_low_information(text):
        return None
    fallback = fallback_extract(comment)
    if llm_client is None or not llm_client.available:
        return fallback
    try:
        result = llm_client.complete(PREFERENCE_SYSTEM_PROMPT, {
            "comment": text,
            "recognized_product": comment.product_name,
            "recognized_category": comment.product_category,
        })
        allowed_preference = {"positive", "negative", "mixed", "neutral"}
        result["preference"] = result.get("preference") if result.get("preference") in allowed_preference else fallback["preference"]
        result["evidence_text"] = str(result.get("evidence_text") or text)[:4000]
        try:
            result["confidence"] = max(0.0, min(1.0, float(result.get("confidence") or 0)))
        except (TypeError, ValueError):
            result["confidence"] = fallback["confidence"]
        for key in (
            "material", "function", "structure", "interaction", "benefit",
            "user_proof", "pain_point", "pet_action", "reason",
        ):
            value = result.get(key)
            result[key] = value if isinstance(value, list) else ([str(value)] if value else [])
        for key in ("shape", "size", "durability"):
            result[key] = str(result.get(key) or fallback.get(key) or "")
        result["extraction_source"] = "llm"
        return result
    except Exception:
        return fallback


def _safe_table_name(table_name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", table_name or ""):
        raise ValueError("不安全的表名")
    return table_name


def ensure_table(conn, table_name: str = "product_preference_events") -> None:
    table_name = _safe_table_name(table_name)
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS `{table_name}` (
              id BIGINT NOT NULL AUTO_INCREMENT,
              comment_id BIGINT NOT NULL,
              material JSON NULL,
              shape VARCHAR(255) NOT NULL DEFAULT '',
              size VARCHAR(255) NOT NULL DEFAULT '',
              `function` JSON NULL,
              structure JSON NULL,
              interaction JSON NULL,
              benefit JSON NULL,
              user_proof JSON NULL,
              pain_point JSON NULL,
              pet_action JSON NULL,
              preference VARCHAR(32) NOT NULL DEFAULT 'neutral',
              reason JSON NULL,
              durability VARCHAR(255) NOT NULL DEFAULT '',
              evidence_text TEXT NOT NULL,
              confidence DECIMAL(5,4) NOT NULL DEFAULT 0,
              extraction_source VARCHAR(32) NOT NULL,
              created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
              PRIMARY KEY (id),
              UNIQUE KEY uq_preference_comment (comment_id),
              KEY idx_preference_sentiment (preference)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        cursor.execute(f"SHOW COLUMNS FROM `{table_name}`")
        columns = {row["Field"] for row in cursor.fetchall()}
        for column in ("structure", "benefit", "user_proof", "pain_point"):
            if column not in columns:
                cursor.execute(f"ALTER TABLE `{table_name}` ADD COLUMN `{column}` JSON NULL AFTER `function`")
        if "product_type" in columns:
            cursor.execute(f"SHOW INDEX FROM `{table_name}` WHERE Key_name='idx_preference_type'")
            if cursor.fetchone():
                cursor.execute(f"ALTER TABLE `{table_name}` DROP INDEX idx_preference_type")
            cursor.execute(f"ALTER TABLE `{table_name}` DROP COLUMN product_type")
    conn.commit()


def save_preference(
    conn, comment_id: int, event: dict[str, Any],
    table_name: str = "product_preference_events",
) -> None:
    table_name = _safe_table_name(table_name)
    ensure_table(conn, table_name)
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            INSERT INTO `{table_name}`(
              comment_id,material,shape,size,`function`,structure,interaction,
              benefit,user_proof,pain_point,pet_action,preference,reason,durability,
              evidence_text,confidence,extraction_source
            ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
              material=VALUES(material),shape=VALUES(shape),size=VALUES(size),
              `function`=VALUES(`function`),structure=VALUES(structure),
              interaction=VALUES(interaction),benefit=VALUES(benefit),
              user_proof=VALUES(user_proof),pain_point=VALUES(pain_point),
              pet_action=VALUES(pet_action),preference=VALUES(preference),reason=VALUES(reason),
              durability=VALUES(durability),evidence_text=VALUES(evidence_text),
              confidence=VALUES(confidence),extraction_source=VALUES(extraction_source),updated_at=NOW()
            """,
            (
                comment_id, json.dumps(event["material"], ensure_ascii=False),
                event["shape"], event["size"], json.dumps(event["function"], ensure_ascii=False),
                json.dumps(event["structure"], ensure_ascii=False),
                json.dumps(event["interaction"], ensure_ascii=False),
                json.dumps(event["benefit"], ensure_ascii=False),
                json.dumps(event["user_proof"], ensure_ascii=False),
                json.dumps(event["pain_point"], ensure_ascii=False),
                json.dumps(event["pet_action"], ensure_ascii=False), event["preference"],
                json.dumps(event["reason"], ensure_ascii=False), event["durability"],
                event["evidence_text"], event["confidence"], event["extraction_source"],
            ),
        )
    conn.commit()
