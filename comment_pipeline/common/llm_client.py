"""Small JSON-only Qwen client shared by the router and extractors."""

from __future__ import annotations

import json
import re
from typing import Any

from openai import OpenAI

from app_config import get_qwen_config


class JsonLlmClient:
    def __init__(self, model: str = "", timeout: float = 45.0):
        self.config = get_qwen_config({"model": model} if model else None)
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self.config["api_key"])

    def complete(self, system_prompt: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.available:
            raise RuntimeError("未配置 DASHSCOPE_API_KEY/QWEN_API_KEY")
        client = OpenAI(
            api_key=self.config["api_key"], base_url=self.config["base_url"], timeout=self.timeout,
        )
        response = client.chat.completions.create(
            model=self.config["model"], temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        )
        raw = str(response.choices[0].message.content or "").strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("模型未返回 JSON 对象")
        return result
