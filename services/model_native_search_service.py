"""Provider-hosted web search only. No local product catalogue or external search service."""
from __future__ import annotations
import os
from urllib.parse import urlsplit
import requests

SEARCH_INSTRUCTIONS = '''只返回合法 JSON，严格沿用用户模板结构；模板为 knowledge_points 时，每个元素是一组原因、机制及其从属字段，不得改成 dimensions。模板为 dimensions 时保留其五维或六维结构。
knowledge_points 各项保留 cause、mechanism、manifestations、verification、nutrition_factors（factor/direction/relation）及 evidence_status；dimensions 各项严格保留模板字段：普通知识的 content、topic、category、related_categories、evidence_status，产品建议的 brand、product_name、reason、applicable_conditions，产品内容的 product_type、life_stage、formula_features、nutrition；不得把产品结构强制改成 content，也不得省略 category 和关联字段。不新增模板未要求的层级，缺失从属数组用 []。
本次已启用模型服务端联网搜索，请实际使用联网工具检索外部资料，不使用本地产品库。
涉及商品选择时，主动搜索品牌官网或官方产品页面，给出能查证的具体产品名称及适用条件；不要仅因用户未提供品牌清单而拒绝检索。
具体型号、配方和数值须以检索资料为依据；不能核实的字段说明未知，不保证一定有足够证据推荐。
可在每条知识中增加 source_urls 数组，只填写实际检索到且支持该条内容的链接。
搜索结果仍可能不可靠，保留不确定性和审核状态，不将搜索命中等同于医学证实。'''


def supports_search(provider):
    host = (urlsplit(provider.base_url).hostname or '').lower()
    if provider.name == 'qwen':
        return host in {'dashscope.aliyuncs.com','dashscope-intl.aliyuncs.com','dashscope-us.aliyuncs.com'} or host.endswith('.maas.aliyuncs.com')
    if provider.name == 'doubao':
        return host == 'ark.cn-beijing.volces.com'
    return provider.name == 'deepseek' and host == 'api.deepseek.com'


def search_model(provider):
    if not supports_search(provider):
        raise ValueError(f'{provider.name} 当前服务地址未适配内置联网搜索')
    if provider.name == 'deepseek':
        model = os.getenv('DEEPSEEK_SEARCH_MODEL') or 'deepseek-v4-pro'
        if not model.startswith('deepseek-v4-pro'):
            raise ValueError('DeepSeek 内置搜索须使用 deepseek-v4-pro 系列，不能使用 flash')
        return model
    if provider.name == 'doubao':
        return (os.getenv('DOUBAO_SEARCH_MODEL') or provider.model).strip()
    return provider.model


def extract_sources(value):
    sources = {}
    def visit(item):
        if isinstance(item, dict):
            url = item.get('url')
            if isinstance(url, str) and urlsplit(url).scheme in ('http','https'):
                sources.setdefault(url, {'url':url,'title':str(item.get('title') or item.get('site_name') or url)})
            for child in item.values(): visit(child)
        elif isinstance(item,list):
            for child in item:visit(child)
    visit(value)
    return list(sources.values())


def call(provider, prompt):
    model = search_model(provider)
    if provider.model != model:
        raise ValueError('任务模型快照与联网模型不一致，请重新提交联网任务')
    if provider.name == 'qwen':
        base = provider.base_url.rstrip('/')
        if not base.endswith('/compatible-mode/v1'):
            raise ValueError('千问联网搜索需要标准百炼 compatible-mode/v1 服务地址')
        url = base[:-len('/compatible-mode/v1')] + '/api/v1/services/aigc/text-generation/generation'
        payload = {'model':model,'input':{'messages':[{'role':'system','content':SEARCH_INSTRUCTIONS},{'role':'user','content':prompt}]},
                   'parameters':{'enable_search':True,'result_format':'message','temperature':0.2,
                                 'search_options':{'forced_search':True,'enable_source':True,'enable_citation':False}}}
    else:
        url = provider.base_url.rstrip('/') + '/responses'
        payload = {'model':model,'instructions':SEARCH_INSTRUCTIONS,'input':prompt,
                   'tools':[{'type':'web_search'}],'tool_choice':'auto',
                   'reasoning':{'effort':'none'},'max_output_tokens':7000}
    if provider.name == 'doubao':
        # Ark uses thinking rather than DeepSeek's reasoning.effort parameter.
        payload.pop('reasoning', None)
        payload['thinking'] = {'type': 'disabled'}
    response = requests.post(url, headers={'Authorization':'Bearer '+provider.key,'Content-Type':'application/json'},
                             json=payload,timeout=float(os.getenv('LLM_SEARCH_TIMEOUT_SECONDS','180')))
    if provider.name == 'doubao' and response.status_code in (400, 403, 404):
        error = (response.json().get('error') or {})
        if error.get('code') in ('ToolNotOpen', 'AccessDenied'):
            raise ValueError('豆包联网内容插件未开通或无访问权限，请在火山方舟控制台开通：'
                             'https://console.volcengine.com/common-buy/CC_content_plugin')
    response.raise_for_status()
    body = response.json()
    if provider.name == 'qwen':
        output = body.get('output') or {}
        choices = output.get('choices') or []
        if not choices:
            raise ValueError('千问未返回正文：'+str(body.get('message') or body.get('code') or 'unknown'))
        text = choices[0]['message'].get('content') or ''
        search_info = output.get('search_info') or body.get('search_info') or {}
        usage = body.get('usage') or {}
        plugins = (usage.get('plugins') or {}).get('search') or {}
        count = plugins.get('count',0) if isinstance(plugins,dict) else 0
        sources = extract_sources(search_info)
        performed = bool(sources or count)
        metadata = {'search_info':search_info,'usage':usage,'request_id':body.get('request_id')}
        input_tokens, output_tokens = usage.get('input_tokens'), usage.get('output_tokens')
    else:
        output = body.get('output') or []
        text = ''.join(part.get('text','') for item in output if item.get('type')=='message'
                       for part in item.get('content',[]) if part.get('type')=='output_text')
        calls = [item for item in output if item.get('type')=='web_search_call']
        annotations = [a for item in output if item.get('type')=='message'
                       for part in item.get('content',[]) for a in part.get('annotations',[])]
        usage = body.get('usage') or {}
        sources = extract_sources([calls,annotations])
        tool_usage = usage.get('server_tool_use') or {}
        performed = bool(tool_usage.get('tool_search') or (usage.get('tool_usage') or {}).get('web_search') or any(c.get('status')=='completed' for c in calls))
        metadata = {'web_search_calls':calls,'annotations':annotations,'usage':usage,
                    'response_id':body.get('id'),'response_status':body.get('status')}
        input_tokens, output_tokens = usage.get('input_tokens'), usage.get('output_tokens')
    metadata.update(requested=True,performed=performed,
                    status='searched' if performed and sources else 'no_sources' if performed else 'not_confirmed',
                    sources=sources,model=model,endpoint=url,
                    instructions=SEARCH_INSTRUCTIONS)
    return text, {'prompt_tokens':input_tokens,'completion_tokens':output_tokens}, metadata
