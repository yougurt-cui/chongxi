"""Direct, lossless ingestion of the health template's cause/mechanism groups."""
from .task_parser import EVIDENCE
VERSION='2026-11-11-v7-object-causes'
CAUSE_TYPES={'diet','feeding_behavior','physiology','environment','disease'}


def validate_document(doc):
    if not isinstance(doc,dict):raise ValueError('健康结果必须是对象')
    for k in ('question','category','problem'):
        if not isinstance(doc.get(k),str):raise ValueError(k+' 必须是字符串')
    for key in ('species','breed','life_stage'):
        if key in doc and doc[key] is not None and not isinstance(doc[key],str):raise ValueError(key+' 必须为字符串或null')
    items=doc.get('knowledge_points')
    if not isinstance(items,list) or not items:raise ValueError('knowledge_points 必须是非空数组')
    for i,p in enumerate(items):
        if not isinstance(p,dict):raise ValueError('健康知识点必须是对象')
        if 'cause_type' in p and p['cause_type'] not in CAUSE_TYPES:raise ValueError('cause_type 原因分类无效')
        for key in ('species','breed','life_stage'):
            if key in p and p[key] is not None and not isinstance(p[key],str):raise ValueError(key+' 必须为字符串或null')
        conditions=p.get('applicable_conditions',[])
        if isinstance(conditions,str):
            conditions=[conditions] if conditions.strip() else []
            p['applicable_conditions']=conditions
        if not isinstance(conditions,list) or any(not isinstance(c,str) for c in conditions):raise ValueError('applicable_conditions 必须为字符串数组')
        for key in ('cause','mechanism'):
            if not isinstance(p.get(key),str) or not p[key].strip():raise ValueError(key+' 必须非空')
        if not isinstance(p.get('manifestations'),list) or any(not isinstance(s,str) or not s.strip() for s in p['manifestations']):
            raise ValueError('manifestations 必须是字符串数组')
        v=p.get('verification')
        if not isinstance(v,dict) or set(v)!={'supporting','opposing','required_information'}:raise ValueError('verification 判断依据结构不正确')
        for values in v.values():
            if not isinstance(values,list) or any(not isinstance(s,str) or not s.strip() for s in values):raise ValueError('判断依据须为字符串数组')
        if p.get('evidence_status') not in EVIDENCE:raise ValueError('证据状态无效')
        factors=p.get('nutrition_factors')
        if not isinstance(factors,list) or len(factors)>3:raise ValueError('每个原因的 nutrition_factors 必须为不超过3项的数组')
        for f in factors:
            if not isinstance(f,dict) or not isinstance(f.get('factor'),str) or not f['factor'].strip():raise ValueError('营养指标编码不能为空')
            if f.get('direction') not in ('increase','decrease','change','unknown','stable'):raise ValueError('营养指标方向无效')
            if 'relation' in f:
                if not isinstance(f['relation'],str) or not f['relation'].strip():raise ValueError('营养关联 relation 必须非空')
            elif not all(isinstance(f.get(k),str) for k in ('explanation','condition')):
                raise ValueError('营养关联需有 relation，或旧版 explanation/condition')
        urls=p.get('source_urls',[])
        if not isinstance(urls,list) or any(not isinstance(u,str) or not u.startswith(('http://','https://')) for u in urls):raise ValueError('来源链接格式无效')
    return doc


def source_rows(doc):
    validate_document(doc)
    return [{'source_id':'knowledge_points:'+str(i),'source_dimension':'knowledge_points','source_index':i,
        'content':p['cause']+'\n'+p['mechanism'],'source_topic':doc['problem'],
        'applicable_conditions':p.get('applicable_conditions',[]),'evidence_status':p['evidence_status'],
        'source_urls':p.get('source_urls',[]),'nutrition_factors':p['nutrition_factors']}
        for i,p in enumerate(doc['knowledge_points'])]


def to_points(doc,enums,resolve_issue):
    sources=source_rows(doc);categories,issues,_=enums
    issue=resolve_issue(doc,enums)
    if issue not in issues:raise ValueError('健康问题无法匹配启用字典，原文保留待重试')
    category=issues[issue]['category_code']
    if category not in categories:raise ValueError('健康分类与问题字典不匹配')
    points=[]
    for source,raw in zip(sources,doc['knowledge_points']):
        sid=source['source_id'];quote=lambda text:{'source_id':sid,'text':text}
        reasons=['候选原因及机制待人工审核，不代表已经确诊或完成证据核验']
        if doc['category']!=category or doc.get('problem_code',doc['problem'])!=issue:
            reasons.append('原模型分类/问题已映射为数据库标准枚举，原值保留于 source_items')
        if raw['nutrition_factors']:reasons.append('营养关联按原文保留，需核验标准编码及适用条件，不代表已经完成科学证据核验')
        if '或' in raw['cause']:reasons.append('原原因含备选因素，需人工核对是否进一步拆分；从属内容保持原组关联')
        points.append({**source,'category_code':category,'category_name':categories[category]['name'],
            'issue_code':issue,'problem_code':issue,'knowledge_type':None,'cause_type':raw.get('cause_type'),'cause':raw['cause'],'mechanism':raw['mechanism'],
            'manifestations':raw['manifestations'],'verification':raw['verification'],'review_reasons':reasons,'review_status':'pending',
            'source_items':[{'source_id':sid,'header':{k:doc.get(k) for k in ('question','species','breed','life_stage','category','problem','problem_code')},'raw_point':raw}],
            'source_refs':{'cause':quote(raw['cause']),'mechanism':quote(raw['mechanism']),
                'manifestations':[quote(s) for s in raw['manifestations']],
                'verification':{k:[quote(s) for s in vals] for k,vals in raw['verification'].items()}},
            'nutrition_source_refs':[{'source_id':sid,'factor_index':i} for i in range(len(raw['nutrition_factors']))]})
    return points
