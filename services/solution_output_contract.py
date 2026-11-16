"""Output contract and conservative adapters for editable solution prompts."""
from copy import deepcopy
import hashlib
import json
import re
from pathlib import Path

CONTRACT_PATH = Path(__file__).resolve().parents[1] / 'vendor/chongxi_multi_model_mysql/templates/solution_output_contract.txt'


def render_prompt(template, question):
    prompt = template.replace('{{question}}', question)
    for key in ('species', 'breed', 'life_stage', 'need'):
        prompt = prompt.replace('{{'+key+'}}', '未提供；仅根据问题明确的信息填写')
    prompt = prompt.replace('{{scenario_count}}', '5')
    return prompt + '\n\n' + CONTRACT_PATH.read_text(encoding='utf-8') + '\n原始问题（question须原样返回）：' + json.dumps(question, ensure_ascii=False)


def adapt_document(document, *, question=None):
    """Fill transport metadata and unambiguous aliases, never invent medical content."""
    result = deepcopy(document)
    if result.get('knowledge_base') not in (None, 'solution'):
        raise ValueError('解决方案响应的knowledge_base与当前任务不一致')
    result.setdefault('knowledge_base', 'solution')
    if result['knowledge_base'] is None:result['knowledge_base']='solution'
    if not result.get('question') and question:result['question']=question
    points=result.get('solutions')
    if not isinstance(points,list):return result
    for point in points:
        if not isinstance(point,dict) or not ('scenario' in point or 'scenario_code' in point):continue
        species=point.get('species')
        if isinstance(species,str):
            species=species.strip().lower()
            point['species']={'feline':'cat','canine':'dog','猫':'cat','猫咪':'cat','犬':'dog','狗':'dog','狗狗':'dog'}.get(species,species)
        for key in ('breed','life_stage'):
            if point.get(key) in (None,''):point[key]='unspecified'
        # Exact original scenario identity only: no fuzzy/semantic equivalence claim.
        scenario=point.get('scenario')
        if not point.get('scenario_code') and isinstance(scenario,str) and scenario.strip():
            canonical=re.sub(r'\s+',' ',scenario).strip()
            point['scenario_code']='legacy_'+hashlib.sha256(canonical.encode()).hexdigest()[:32]
        evidence=point.get('evidence_status')
        if evidence in (None,'','common_sense_pending_review','veterinary_consensus_pending_review'):
            point['evidence_status']='pending_verification'
        if point.get('source_urls') is None:point['source_urls']=[]
    return result


def decode_quoted_solution(value):
    """Repair only paired unescaped CJK quotation marks in a JSON string value.

    No text is deleted/rephrased. Missing commas, brackets, truncation and other
    JSON errors remain failures. Returned offsets refer to each repair's input.
    """
    repairs=[]
    decoder=json.JSONDecoder()
    for _ in range(8):
        try:
            obj,_=decoder.raw_decode(value)
            return obj,repairs
        except json.JSONDecodeError as exc:
            if exc.msg != "Expecting ',' delimiter":raise
            opening=exc.pos-1
            if opening<1 or value[opening]!='"' or not re.fullmatch(r'[\u3400-\u9fff]',value[opening-1]):raise
            match=re.match(r'[\u3400-\u9fff]{1,80}"(?=[\u3400-\u9fff，、。；：！？（）])',value[exc.pos:])
            if not match:raise
            closing=exc.pos+len(match.group())-1
            repairs.append({'type':'escape_paired_cjk_quotes','opening_offset':opening,'closing_offset':closing})
            value=value[:opening]+'\\'+value[opening:closing]+'\\'+value[closing:]
    raise ValueError('模型JSON引号修复超过上限，请重新生成合法JSON')
