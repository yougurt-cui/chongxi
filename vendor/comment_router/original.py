#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""宠析评论双维度路由器：内容领域 + 业务处理器。

Python 3.9+，仅使用标准库。

使用示例：
  export LLM_API_KEY='your-api-key'
  export LLM_BASE_URL='https://api.openai.com/v1'
  export LLM_MODEL='your-model-name'
  python comment_router.py --text '我家猫吃A粮后软便，换B粮后改善了。'
  python comment_router.py --input comments.jsonl --output result.jsonl
  python comment_router.py --demo   # 不调用模型，仅演示预期输出
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DOMAINS = {
    "pet_food": "宠物食品（主粮、零食、营养补充品、饮食）",
    "pet_supplies": "玩具用品（玩具、猫砂、猫窝、护理用品等非智能用品）",
    "smart_device": "智能硬件（智能猫砂盆、喂食器、饮水机、传感器等）",
    "pet_medical": "宠物医疗（实际就医、检查、诊断、用药、治疗、医疗记录）",
    "daily_care": "日常养宠（行为、生活习惯、日常照护与喂养方法）",
}
PROCESSORS = {
    "product_feedback": "商品反馈：明确商品/品牌对象，并有使用体验或评价",
    "solution_experience": "解决经验：描述实际遇到的问题与已采取的处理措施；结果可未知",
    "user_need": "用户诉求：明确表达目标、期望、未满足需求或购买约束",
    "health_observation": "健康观察：描述具体宠物的症状、健康状态或其变化（非纯科普）",
    "medical_case": "医疗案例：有真实诊疗经历或具体检查/诊断/治疗记录；仅提疾病名称不算",
}

SYSTEM_PROMPT = """你是宠析平台的宠物内容路由分类器。仅对输入内容做分类，不提供诊疗建议，不进行病因推断。

任务1：domains 内容领域，多标签，候选枚举：
- pet_food：宠物食品、主粮、零食、营养补充品、饮食相关
- pet_supplies：玩具、猫砂、猫窝、常规宠物用品、护理用品
- smart_device：智能猫砂盆、喂食器、饮水机、智能传感器等
- pet_medical：实际就诊、检查、确诊、用药、治疗或医疗记录
- daily_care：日常照护、行为、生活习惯、非医疗护理方法
边界：单纯提到软便等症状，不意味着 pet_medical；没有实际就诊/检查/治疗就不要自动加医疗领域。
不要因为评论写了“吃猫粮”就额外添加 daily_care，除非确实谈日常照护方法。

任务2：processors 处理器，多标签，候选枚举：
- product_feedback：有可识别商品/品牌及针对其的实际体验或评价
- solution_experience：有实际问题且已经采取某项处理措施；结果允许未知。单纯求方法不命中。
- user_need：明确表达需求、目标、未满足诉求或购买约束；不要将对产品的单一抱怨自动当作明确求购需求。
- health_observation：具体宠物的症状、健康状态及变化，纯疾病科普不命中
- medical_case：具体医疗诊疗案例，需有就诊/检查/诊断/治疗等实际过程；仅提疾病、担忧或提问不命中

只提取原文可支持的信息。允许一条评论命中多个领域和多个处理器；也允许返回空数组。
对每个命中的领域和处理器均提供原文连续片段作为 evidence，片段必须直接出现于评论中，不能改写。
输出严格 JSON 对象，格式：
{
  "domains": ["pet_food"],
  "processors": ["product_feedback"],
  "domain_evidence": {"pet_food": "吃A粮"},
  "processor_evidence": {"product_feedback": "吃A粮后一直软便"}
}
不要输出 markdown、解释或其它字段。"""


def extract_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for idx, char in enumerate(text):
            if char == "{":
                try:
                    value, _ = decoder.raw_decode(text[idx:])
                    return value
                except json.JSONDecodeError:
                    pass
    raise ValueError("模型输出未包含可解析的 JSON 对象")


def validate_result(obj, content, content_id):
    if not isinstance(obj, dict):
        raise ValueError("模型输出必须为 JSON 对象")
    for k, allowed in (("domains", DOMAINS), ("processors", PROCESSORS)):
        val = obj.get(k)
        if not isinstance(val, list) or len(set(map(str, val))) != len(val):
            raise ValueError(f"{k} 必须是无重复元素的数组")
        invalid = [x for x in val if not isinstance(x, str) or x not in allowed]
        if invalid:
            raise ValueError(f"{k} 存在无效标签：{invalid}")
    for names_key, evidence_key in (("domains", "domain_evidence"), ("processors", "processor_evidence")):
        evidences = obj.get(evidence_key)
        if not isinstance(evidences, dict) or set(evidences) != set(obj[names_key]):
            raise ValueError(f"{evidence_key} 的键必须和 {names_key} 一致")
        for label, quote in evidences.items():
            if not isinstance(quote, str) or not quote.strip() or quote not in content:
                raise ValueError(f"{evidence_key}.{label} 不是原文中的连续片段")
    return {
        "content_id": content_id,
        "domains": obj["domains"],
        "processors": obj["processors"],
        "domain_evidence": obj["domain_evidence"],
        "processor_evidence": obj["processor_evidence"],
        "classification_status": "success",
    }


def call_llm(content, model, base_url, api_key, timeout=60):
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps({"comment": content}, ensure_ascii=False)},
        ],
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as res:
        result = json.loads(res.read().decode("utf-8"))
    message = result["choices"][0]["message"]["content"]
    if isinstance(message, list):
        message = "".join(part.get("text", "") for part in message if isinstance(part, dict))
    return extract_json(message)


def route(content, content_id, model, base_url, api_key, retries=2):
    for attempt in range(retries + 1):
        try:
            raw = call_llm(content, model, base_url, api_key)
            return validate_result(raw, content, content_id)
        except (ValueError, KeyError, IndexError, urllib.error.URLError, TimeoutError) as exc:
            if attempt == retries:
                return {
                    "content_id": content_id,
                    "domains": [],
                    "processors": [],
                    "domain_evidence": {},
                    "processor_evidence": {},
                    "classification_status": "failed",
                    "error": str(exc),
                }
            time.sleep(min(2 ** attempt, 4))


DEMO_CONTENT = "我家布偶猫吃A粮后经常软便，后来换成B粮好了很多，但B粮太贵了，想找便宜一些的替代粮。"
DEMO_RESULT = {
    "content_id": "demo_001",
    "domains": ["pet_food"],
    "processors": ["product_feedback", "solution_experience", "user_need", "health_observation"],
    "domain_evidence": {"pet_food": "吃A粮"},
    "processor_evidence": {
        "product_feedback": "吃A粮后经常软便",
        "solution_experience": "后来换成B粮好了很多",
        "user_need": "想找便宜一些的替代粮",
        "health_observation": "经常软便",
    },
    "classification_status": "success",
}


def main():
    parser = argparse.ArgumentParser(description="宠析评论：领域识别与多处理器匹配")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--text", help="单条评论")
    group.add_argument("--input", help="输入 JSONL，格式每行 {\"content_id\":\"...\",\"content\":\"...\"}")
    group.add_argument("--demo", action="store_true", help="输出示例（不调用模型）")
    parser.add_argument("--output", help="结果 JSONL 文件路径；不传则打印到标准输出")
    parser.add_argument("--model", default=os.environ.get("LLM_MODEL", ""))
    parser.add_argument("--base-url", default=os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1"))
    parser.add_argument("--api-key", default=os.environ.get("LLM_API_KEY", ""))
    args = parser.parse_args()

    if args.demo:
        print("示例评论：" + DEMO_CONTENT)
        print(json.dumps(DEMO_RESULT, ensure_ascii=False, indent=2))
        return
    if not args.api_key or not args.model:
        parser.error("请设置 LLM_API_KEY 和 LLM_MODEL，或通过命令行传入对应参数")

    records = [{"content_id": "comment_001", "content": args.text}] if args.text else []
    if args.input:
        for line_no, line in enumerate(Path(args.input).read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            item = json.loads(line)
            content = item.get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError(f"输入第 {line_no} 行缺少非空 content")
            records.append({"content_id": str(item.get("content_id", f"comment_{line_no}")), "content": content})

    lines = []
    for item in records:
        result = route(item["content"], item["content_id"], args.model, args.base_url, args.api_key)
        lines.append(json.dumps(result, ensure_ascii=False))
    if args.output:
        Path(args.output).write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"已保存 {len(lines)} 条结果：{args.output}")
    else:
        for line in lines:
            print(json.dumps(json.loads(line), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
