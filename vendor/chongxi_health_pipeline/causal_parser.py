"""Validate one source-grounded cause/mechanism chain per health knowledge row."""
from .task_parser import digest
VERSION = '2026-11-10-v5.1-causal'


def parse_chains(sources, question, enums, extract):
    categories,issues,_=enums
    source_map={s['source_id']:s for s in sources}
    value=extract(sources,question,enums)
    if not isinstance(value,dict) or set(value)!={'chains','excluded'} or not isinstance(value['chains'],list) or not isinstance(value['excluded'],list):
        raise ValueError('因果解析须返回 chains 和 excluded 数组')
    def quote(q):
        if not isinstance(q,dict) or set(q)!={'source_id','text'}:raise ValueError('引用须含 source_id/text')
        sid=q['source_id'];text=q['text']
        if not isinstance(sid,str) or sid not in source_map or not isinstance(text,str) or not text.strip():raise ValueError('引用来源或正文无效')
        s=source_map[sid]
        source_texts=[s['content'],*s['applicable_conditions'],
                      *[f[k] for f in s.get('nutrition_factors',[]) for k in ('explanation','condition')]]
        if not any(text in original for original in source_texts):
            raise ValueError('因果字段不是原文逐字摘录：'+text[:60])
        return dict(q)
    covered=set();points=[];keys=set()
    for raw in value['chains']:
        if not isinstance(raw,dict) or set(raw)!={'issue_code','cause','mechanism','manifestations','verification','nutrition_refs'}:
            raise ValueError('因果链字段不正确')
        issue=raw['issue_code']
        if not isinstance(issue,str) or issue not in issues:raise ValueError('健康问题不在启用字典中')
        category=issues[issue]['category_code']
        if category not in categories:raise ValueError('健康问题的分类关联无效')
        cause=quote(raw['cause']);mechanism=quote(raw['mechanism'])
        if cause['text'] in mechanism['text'] or mechanism['text'] in cause['text']:
            raise ValueError('原因和机制不能互相包含或重复，原文无独立机制时列excluded')
        if '或' in cause['text'] or '、' in cause['text']:
            raise ValueError('每条原因必须单一，多个备选原因须拆成多条chain')
        # A chain must have source-local causal support; cross-item synthesis can imply false causality.
        if cause['source_id']!=mechanism['source_id']:raise ValueError('原因和机制必须由同一来源条目明确支持')
        if not isinstance(raw['manifestations'],list):raise ValueError('伴随表现必须是数组')
        manifestations=[quote(q) for q in raw['manifestations']]
        verification=raw['verification']
        if not isinstance(verification,dict) or set(verification)!={'supporting','opposing','required_information'}:
            raise ValueError('判断依据结构不正确')
        checks={}
        for key,items in verification.items():
            if not isinstance(items,list):raise ValueError('判断依据必须是数组')
            checks[key]=[quote(q) for q in items]
        refs={q['source_id'] for q in [cause,mechanism,*manifestations,*[q for items in checks.values() for q in items]]}
        if any(sid!=cause['source_id'] for sid in refs):raise ValueError('从属字段必须来自同一条原因机制来源，禁止跨原因嫁接')
        if not isinstance(raw['nutrition_refs'],list):raise ValueError('营养指标引用必须是数组')
        factors=[];nutrition_refs=[]
        for ref in raw['nutrition_refs']:
            if not isinstance(ref,dict) or set(ref)!={'source_id','factor_index'}:raise ValueError('营养指标引用格式错误')
            sid=ref['source_id'];index=ref['factor_index']
            if sid not in refs or type(index) is not int or not 0<=index<len(source_map[sid]['nutrition_factors']):
                raise ValueError('营养指标必须引用该因果链来源中的已有指标')
            if ref not in nutrition_refs:
                nutrition_refs.append(ref);factors.append(source_map[sid]['nutrition_factors'][index])
        anchor=source_map[cause['source_id']]
        key=digest({'issue':issue,'cause':cause,'mechanism':mechanism,'manifestations':manifestations,'verification':checks,'nutrition':nutrition_refs})
        covered.update(refs)
        if key in keys:continue
        keys.add(key)
        reasons=['候选因果关系须人工复核，摘录不代表证据已核验']
        if not manifestations:reasons.append('原文未明确该原因对应的伴随表现')
        if not any(checks.values()):reasons.append('原文未明确该原因对应的判断依据')
        if factors:reasons.append('营养指标按原文关联，编码及因果有效性待核验')
        points.append({**anchor,'source_id':'chain:'+key,'issue_code':issue,'problem_code':issue,
            'category_code':category,'category_name':categories[category]['name'],'knowledge_type':None,
            'cause':cause['text'],'mechanism':mechanism['text'],
            'manifestations':[q['text'] for q in manifestations],
            'verification':{k:[q['text'] for q in items] for k,items in checks.items()},
            'nutrition_factors':factors,'nutrition_source_refs':nutrition_refs,
            'source_refs':{'cause':cause,'mechanism':mechanism,'manifestations':manifestations,'verification':checks},
            'source_items':[source_map[sid] for sid in sorted(refs)],
            'content':cause['text']+'\n'+mechanism['text'],
            'review_status':'pending','review_reasons':reasons})
    excluded=[];excluded_ids=set()
    for item in value['excluded']:
        if not isinstance(item,dict) or set(item)!={'source_id','reason'}:raise ValueError('未成链条目须记录来源和原因')
        sid=item['source_id']
        if sid not in source_map or sid in excluded_ids or sid in covered or not isinstance(item['reason'],str) or not item['reason'].strip():
            raise ValueError('未成链条目重复、无效或与因果链冲突')
        excluded_ids.add(sid);excluded.append(item)
    if covered|excluded_ids!=set(source_map):raise ValueError('因果解析遗漏来源条目，必须明确列入链或未成链清单')
    return points,excluded
