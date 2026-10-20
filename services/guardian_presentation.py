"""Small safe display protocol: text, emphasis and explicit semantic markers."""
import re

KEY_TERMS = re.compile(r"(精神明显变差|频繁呕吐|便血|尽快联系宠物医院|需要优先评估|资料不足|待核对|实际开始吃|共同特点|潜在短板|需要留意)")

STYLE_INSTRUCTION = (
    "回答用短段落，每段一到两句话。每段至少选择一个2到12字的重点短语用**加粗**，不要整段加粗。"
    "有证据的有利项必须以✅开头，明确不利项必须以❌开头，不确定性及观察事项必须以❗开头；"
    "同一段包含不同性质的信息时用换行拆段，各段分别标记；中性的背景和操作步骤可以不加标记。"
    "不强行凑齐三类，不把资料不足说成不利，不添加未提供的事实。"
)


def display_blocks(text, response_type=None):
    blocks = []
    for paragraph in re.split(r"\n+", str(text or "")):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        # Split long prose at sentence boundaries, never truncate its content.
        chunks, chunk, sentence_count = [], "", 0
        for sentence in re.findall(r"[^。！？!?]+[。！？!?]*|[。！？!?]+", paragraph):
            if chunk and (len(chunk) + len(sentence) > 160 or sentence_count >= 2):
                chunks.append(chunk)
                chunk = ""
                sentence_count = 0
            chunk += sentence
            sentence_count += 1
        if chunk:
            chunks.append(chunk)
        for text_chunk in chunks or [paragraph]:
            marker = re.match(r"^(?:\d+[.、)]\s*)?(✅|❌|❗)\s*", text_chunk)
            kind = {"✅": "positive", "❌": "negative", "❗": "caution"}.get(marker[1]) if marker else "caution" if response_type == "risk_alert" else "body"
            body = text_chunk[marker.end():] if marker else text_chunk
            segments = []
            for part in re.split(r"(\*\*[^*\n]+\*\*)", body):
                if part:
                    strong = part.startswith("**") and part.endswith("**") and len(part) > 4
                    if strong:
                        segments.append({"text": part[2:-2], "strong": True})
                    else:
                        segments.extend({"text": term, "strong": bool(KEY_TERMS.fullmatch(term))}
                                        for term in KEY_TERMS.split(part.replace('**', '')) if term)
            blocks.append({"kind": kind, "marker": marker[1] if marker else "❗" if kind == "caution" else "", "segments": segments})
    return blocks


def present_message(message):
    if message.get("role") == "assistant":
        result = dict(message.get("result") or {})
        result["display_version"] = 1
        text = message.get("content") or message.get("reply")
        recommendation = result.get('recommendation') or {}
        # Existing clients render both blocks and candidate cards. Keep the full
        # content for clients without blocks, but avoid displaying every card twice.
        if result.get('food_candidates') and recommendation.get('status') == 'ready' and recommendation.get('display_intro'):
            text = recommendation['display_intro']
        result["display_blocks"] = display_blocks(text, message.get("response_type"))
        message = {**message, "result": result}
    return message
