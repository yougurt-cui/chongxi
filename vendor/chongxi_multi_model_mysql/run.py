#!/usr/bin/env python3
"""宠析三模型知识采集：并发调用 Qwen/豆包/DeepSeek，写入 MySQL 8+。

此程序只做第一阶段原子候选知识采集，不会自动合并为正式知识卡，
也不会根据模型自评把知识设为审核通过。
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import logging
import os
import re
import time
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pymysql
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Module overrides are optional; process environment takes precedence.
load_dotenv(ROOT / '.env', override=False)
from app_config import get_mysql_config, get_qwen_config, normalize_openai_base_url
DIMENSIONS = (
    'applicable_object', 'selection_points', 'product_recommendations',
    'product_content', 'precautions',
)
VALID_EVIDENCE = {'supported', 'uncertain', 'unknown'}


@dataclass(frozen=True)
class Provider:
    name: str
    key: str
    base_url: str
    model: str


def mysql_connect():
    from services.model_knowledge_import_service import connect
    return connect()


def initialize_schema():
    from services.model_knowledge_import_service import ensure_database
    return ensure_database()



def provider_configuration():
    qwen = get_qwen_config()
    return [
        Provider('qwen', qwen['api_key'], qwen['base_url'], qwen['model']),
        Provider('doubao',
                 (os.getenv('DOUBAO_API_KEY') or os.getenv('ARK_API_KEY')
                  or os.getenv('VOLCENGINE_ARK_API_KEY') or '').strip(),
                 normalize_openai_base_url(
                     os.getenv('DOUBAO_BASE_URL') or os.getenv('ARK_BASE_URL'),
                     default='https://ark.cn-beijing.volces.com/api/v3'),
                 (os.getenv('DOUBAO_MODEL') or os.getenv('ARK_TEXT_MODEL') or '').strip()),
        Provider('deepseek', (os.getenv('DEEPSEEK_API_KEY') or '').strip(),
                 normalize_openai_base_url(os.getenv('DEEPSEEK_BASE_URL'),
                                           default='https://api.deepseek.com'),
                 (os.getenv('DEEPSEEK_MODEL') or 'deepseek-flash').strip()),
    ]


def providers_from_env():
    providers = provider_configuration()
    missing = []
    for provider in providers:
        if not provider.key:
            missing.append(f'{provider.name}: API Key')
        if not provider.model:
            missing.append(f'{provider.name}: text model ID (DOUBAO_MODEL for doubao)')
    if missing:
        raise ValueError('Missing configuration: ' + '; '.join(missing))
    return providers


def check_configuration():
    """Report presence only; never print credentials or make external calls."""
    complete = True
    for provider in provider_configuration():
        print(f"{provider.name}: key={'configured' if provider.key else 'missing'}, "
              f"model={provider.model or 'MISSING (set DOUBAO_MODEL)'}")
        complete = complete and bool(provider.key and provider.model)
    mysql = get_mysql_config(database=os.getenv('MULTI_MODEL_MYSQL_DATABASE') or None)
    print('mysql: ' + ', '.join(
        f"{field}={'configured' if mysql.get(field) else 'missing'}"
        for field in ('host', 'user', 'password', 'database')))
    import importlib.util
    sdk_present = importlib.util.find_spec('openai') is not None
    print('openai SDK: ' + ('installed' if sdk_present else 'missing'))
    return complete and sdk_present


def extract_json(text: str) -> dict[str, Any]:
    value = text.strip()
    if value.startswith('```'):
        value = re.sub(r'^```(?:json)?\s*', '', value, flags=re.I)
        value = re.sub(r'\s*```$', '', value)
    try:
        obj = json.loads(value)
    except json.JSONDecodeError:
        # 部分模型会添加前言；只寻找首个合法完整对象，避免贪婪正则截断。
        decoder = json.JSONDecoder()
        obj = None
        for i, char in enumerate(value):
            if char != '{':
                continue
            try:
                trial, _ = decoder.raw_decode(value[i:])
            except json.JSONDecodeError:
                continue
            if isinstance(trial, dict):
                obj = trial
                break
        if obj is None:
            raise ValueError('模型未输出可解析的 JSON 对象')
    if isinstance(obj, dict) and 'knowledge_points' in obj:
        from vendor.chongxi_health_pipeline.structured_parser import validate_document
        return validate_document(obj)
    if not isinstance(obj, dict) or not isinstance(obj.get('dimensions'), dict):
        raise ValueError('缺少 dimensions 对象')
    dims = obj['dimensions']
    from vendor.chongxi_health_pipeline.task_parser import dimension_names, EVIDENCE
    dimensions = dimension_names(dims)
    # Selection v2 uses reason/product fields rather than content on product rows.
    if set(dims) == set(DIMENSIONS) and any(isinstance(items,list) and any(isinstance(item,dict) and 'category' in item for item in items) for items in dims.values()):
        from vendor.chongxi_knowledge_importer.import_knowledge import to_rows
        to_rows(obj)
        for items in dims.values():
            for item in items:
                if item.get('evidence_status','unknown') not in EVIDENCE | {'pending_verification'}:
                    raise ValueError('选粮知识 evidence_status 非法')
        return obj
    for dimension in dimensions:
        items = dims.get(dimension)
        if not isinstance(items, list):
            raise ValueError(f'{dimension} 必须是数组')
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                raise ValueError(f'{dimension}[{i}] 必须是对象')
            content = item.get('content')
            if not isinstance(content, str) or not content.strip():
                raise ValueError(f'{dimension}[{i}].content 不可为空')
            if not isinstance(item.get('topic', ''), str):
                raise ValueError(f'{dimension}[{i}].topic 必须是字符串')
            conditions = item.get('applicable_conditions', [])
            if not isinstance(conditions, list) or not all(isinstance(x, str) for x in conditions):
                raise ValueError(f'{dimension}[{i}].applicable_conditions 必须是字符串数组')
            if item.get('evidence_status', 'unknown') not in EVIDENCE:
                raise ValueError(f'{dimension}[{i}].evidence_status 非法')
    return obj


def call_provider(provider: Provider, prompt: str, web_search: bool = False) -> dict[str, Any]:
    started = time.perf_counter()
    response_text = None
    usage = {}
    search_metadata = {'requested': web_search, 'performed': False, 'status': 'failed' if web_search else 'disabled', 'sources': []}
    try:
        if web_search:
            from services.model_native_search_service import call
            response_text, usage, search_metadata = call(provider, prompt)
        else:
            from openai import OpenAI
            client = OpenAI(
                api_key=provider.key,
                base_url=provider.base_url,
                timeout=float(os.getenv('LLM_TIMEOUT_SECONDS', '90')),
                max_retries=int(os.getenv('LLM_MAX_RETRIES', '2')),
            )
            response = client.chat.completions.create(
                model=provider.model,
                messages=[
                    {'role': 'system', 'content': '只返回合法 JSON。对未知或不可靠信息明确保留不确定性。'},
                    {'role': 'user', 'content': prompt},
                ],
                temperature=0.2,
                # Knowledge collection needs structured output within the task timeout.
                **({'extra_body': {'thinking': {'type': 'disabled'}}}
                   if provider.name == 'doubao' else {}),
            )
            response_text = response.choices[0].message.content or ''
            if response.usage:
                usage = {
                    'prompt_tokens': response.usage.prompt_tokens,
                    'completion_tokens': response.usage.completion_tokens,
                }
        try:
            parsed = extract_json(response_text)
            status, error = 'success', None
        except Exception as exc:
            parsed, status, error = None, 'parse_failed', str(exc)
    except Exception as exc:
        safe_message = str(exc).replace(provider.key, '[REDACTED]') if provider.key else str(exc)
        parsed, status, error = None, 'api_failed', f'{type(exc).__name__}: {safe_message}'
    return {
        'provider': provider.name, 'model': provider.model,
        'status': status, 'raw_text': response_text, 'parsed': parsed,
        'error': error, 'search_metadata': search_metadata,
        'latency_ms': int((time.perf_counter() - started) * 1000),
        **usage,
    }


def persist_result(task_id,result):
    from services.model_knowledge_import_service import persist_model_result
    persist_model_result(task_id,result)


def update_task(task_id,status):
    if status in ('failed','partial_failed'):
        from services.multi_model_knowledge_service import fail_running
        fail_running(task_id)


def main():
    parser=argparse.ArgumentParser(description='多模型采集，全部数据仅写入独立知识库的两张表')
    parser.add_argument('--question')
    parser.add_argument('--template',default=str(ROOT/'prompt_template.txt'))
    parser.add_argument('--providers',nargs='+',choices=['qwen','deepseek','doubao'],default=['qwen','deepseek'])
    parser.add_argument('--web-search',action=argparse.BooleanOptionalAction,default=True)
    parser.add_argument('--check-config',action='store_true')
    args=parser.parse_args()
    if args.check_config:return 0 if check_configuration() else 1
    if not args.question:parser.error('--question is required')
    from services.multi_model_knowledge_service import create_task,run_worker,task_detail
    created=create_task({'question':args.question,'template':Path(args.template).read_text(encoding='utf-8'),
                         'providers':args.providers,'web_search':args.web_search},launch=False)
    print(f"创建任务 task_id={created['task_id']}",flush=True)
    run_worker(created['task_id'])
    return 0 if task_detail(created['task_id'])['task']['status']=='completed' else 1


def count_points(parsed):
    """Count validated health groups or dimension items without assuming one schema."""
    if not parsed:
        return 0
    if 'knowledge_points' in parsed:
        return len(parsed['knowledge_points'])
    return sum(len(items) for items in parsed.get('dimensions', {}).values())


def execute_task(task_id: int, prompt: str, providers: list[Provider], on_result=None, web_search=False):
    """Shared CLI/web execution path; each provider result is committed separately."""
    statuses = []
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(providers)) as pool:
            futures = {pool.submit(call_provider, p, prompt, **({'web_search': True} if web_search else {})): p for p in providers}
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                persist_result(task_id, result)
                statuses.append(result['status'])
                if on_result is not None and result['status'] == 'success':
                    try:
                        on_result(task_id)
                    except Exception:
                        logging.exception('Independent knowledge import failed; collection response retained')
                count = count_points(result.get('parsed'))
                print(f"{result['provider']}: {result['status']}，候选知识点={count}，耗时={result['latency_ms']}ms")
                if result['error']:
                    logging.warning('%s: %s', result['provider'], result['error'])
    except Exception:
        update_task(task_id, 'partial_failed')
        raise
    task_status = ('completed' if all(x == 'success' for x in statuses)
                   else 'failed' if all(x != 'success' for x in statuses)
                   else 'partial_failed')
    update_task(task_id, task_status)
    print(f'任务完成: task_id={task_id}, status={task_status}')
    return task_status


if __name__ == '__main__':
    logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(message)s')
    raise SystemExit(main())
