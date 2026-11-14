"""Lossless parsing of the five dimensions emitted by the multi-model health page."""
import hashlib
import json

VERSION = '2026-11-10-v5.1-causal'
DIMENSIONS = ('applicable_object','selection_points','product_recommendations','product_content','precautions')
SIX_DIMENSIONS = ('daily_feeding','care_behavior','ingredients','nutrients','health_risks','products')
EVIDENCE = {'supported','uncertain','unknown','conditional','limited','insufficient'}


def dimension_names(dims):
    if set(dims) == set(DIMENSIONS):return DIMENSIONS
    if set(dims) == set(SIX_DIMENSIONS):return SIX_DIMENSIONS
    raise ValueError('dimensions 必须完整匹配五维或六维结构，不能混用或遗漏')


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def sources_from_dimensions(doc):
    if isinstance(doc,dict) and 'knowledge_points' in doc:
        from .structured_parser import source_rows
        return source_rows(doc)
    if not isinstance(doc,dict) or not isinstance(doc.get('dimensions'),dict):
        raise ValueError('健康任务缺少 dimensions 对象')
    result=[]
    for dimension in dimension_names(doc['dimensions']):
        items=doc['dimensions'].get(dimension)
        if not isinstance(items,list):raise ValueError(dimension+' 必须是数组')
        for index,item in enumerate(items):
            if not isinstance(item,dict) or not isinstance(item.get('content'),str) or not item['content'].strip():
                raise ValueError('健康条目 content 必须是非空字符串')
            conditions=item.get('applicable_conditions',[])
            if not isinstance(conditions,list) or any(not isinstance(x,str) for x in conditions):
                raise ValueError('applicable_conditions 必须是字符串数组')
            evidence=item.get('evidence_status','unknown')
            if evidence not in EVIDENCE:raise ValueError('非法 evidence_status')
            if not isinstance(item.get('topic',''),str):raise ValueError('topic 必须是字符串')
            urls=item.get('source_urls',[])
            if not isinstance(urls,list) or any(not isinstance(x,str) or not x.startswith(('https://','http://')) for x in urls):
                raise ValueError('source_urls 必须是 HTTP 链接数组')
            factors=item.get('nutrition_factors',[])
            if not isinstance(factors,list):raise ValueError('nutrition_factors 必须是数组')
            for factor in factors:
                if not isinstance(factor,dict) or not all(isinstance(factor.get(k),str) for k in ('factor','direction','explanation','condition')):
                    raise ValueError('营养指标需包含 factor/direction/explanation/condition 字符串')
                if not factor['factor'].strip() or factor['direction'] not in ('increase','decrease','change','stable','unknown'):
                    raise ValueError('营养指标编码或变化方向无效')
            result.append({'nutrition_factors':factors,'source_id':dimension+':'+str(index),'source_dimension':dimension,
                           'source_index':index,'content':item['content'],'source_topic':item.get('topic',''),
                           'applicable_conditions':conditions,'evidence_status':evidence,'source_urls':urls})
    if not result:raise ValueError('健康任务所有维度均为空，拒绝静默成功')
    return result


def exact_problem(text, problems):
    # No substring/longest-alias guessing: multiple-problem text requires review.
    text=text.strip()
    matches={code for code,p in problems.items() if text in [code,p['name'],*p.get('aliases',[])]}
    return next(iter(matches)) if len(matches)==1 else None


def classify_sources(sources, question, enums, classify=None):
    categories,problems,types=enums
    predictions={}
    if classify is not None:
        value=classify(sources,question,enums)
        if not isinstance(value,dict) or set(value)!={'results'} or not isinstance(value['results'],list):
            raise ValueError('健康分类模型输出缺少 results')
        known={s['source_id'] for s in sources}
        for r in value['results']:
            if not isinstance(r,dict) or set(r)!={'source_id','problem_code','knowledge_type'}:
                raise ValueError('健康分类结果结构不正确')
            sid=r['source_id']
            if not isinstance(sid,str) or sid not in known or sid in predictions:
                raise ValueError('健康分类结果编号重复或无效')
            for key,allowed in [('problem_code',problems),('knowledge_type',types)]:
                if r[key] is not None and (not isinstance(r[key],str) or r[key] not in allowed):
                    raise ValueError('健康分类结果包含非法枚举: '+key)
            predictions[sid]=r
        if set(predictions)!=known:raise ValueError('健康分类结果遗漏来源条目')
    points=[]
    for s in sources:
        predicted=predictions.get(s['source_id'],{})
        code=predicted.get('problem_code')
        if classify is None:code=exact_problem(s['source_topic'],problems)
        kind=predicted.get('knowledge_type')
        if classify is None and s['source_dimension']=='applicable_object':kind='CONDITION'
        if code is not None and problems[code]['category_code'] not in categories:
            raise ValueError('健康问题与分类字典关联无效')
        reasons=[]
        if s.get('nutrition_factors'):reasons.append('营养指标编码及关联按原模型保留，尚未通过营养指标枚举核验')
        if not code:reasons.append('健康问题未明确映射，需审核')
        if not kind:reasons.append('内容无法唯一归入五种知识类型，保留原文待审核（护理/产品建议不强制作为病因）')
        points.append({**s,'problem_code':code,'category_code':problems[code]['category_code'] if code else None,
                       'knowledge_type':kind,'review_required':True,'review_reasons':reasons,
                       'review_status':'pending'})
    return points
