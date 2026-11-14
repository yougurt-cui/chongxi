"""Dictionary-bound, deterministic object scope; no additional model calls."""
import re


def load_dictionary():
    import pymysql
    from app_config import get_mysql_config
    cfg=get_mysql_config(database='chongxi_knowledge_dictionary')
    cfg['cursorclass']=pymysql.cursors.DictCursor
    with pymysql.connect(**cfg) as conn,conn.cursor() as cur:
        result={}
        for key,table in [('species','dict_species'),('breeds','dict_breed'),('stages','dict_life_stage')]:
            cur.execute('SELECT * FROM `'+table+'`')
            rows=[r for r in cur.fetchall() if r.get('status') in (None,'启用','enabled','active')]
            result[key]=sorted(rows,key=lambda row:tuple(str(row.get(k,'')) for k in sorted(row)))
    return result


def _contains(text,term):
    if not term:return False
    return bool(re.search(r'(?<![a-z0-9_])'+re.escape(term)+r'(?![a-z0-9_])',text,re.I)) if term.isascii() else term in text


def _breeds(text,dictionary):
    found=set()
    for row in dictionary['breeds']:
        if row['breed_code'].startswith(('all_','unknown_')):continue
        names=[row['breed_code'],row['breed_name'],*row.get('aliases_pipe','').split('|')]
        # Common omission of 猫/犬 is still the same explicit dictionary breed.
        names += [n[:-1] for n in names if n.endswith(('猫','犬')) and len(n)>2]
        if any(_contains(text,n) for n in names):found.add((row['species_code'],row['breed_code']))
    return found


def resolve_scope(question,doc,point,dictionary):
    """Per-cause explicit scope overrides header; question supplies missing context.

    Unresolved/multiple scopes stay pending, never expand into a Cartesian product.
    Age is read only from scope fields/conditions/question, not hypothetical
    manifestations or verification statements.
    """
    notes=[]
    conditions=point.get('applicable_conditions',[])
    explicit={k:point.get(k,doc.get(k)) for k in ('species','breed','life_stage')}
    question_breeds=_breeds(question,dictionary)
    scope_text='；'.join([*conditions,*[v for v in explicit.values() if isinstance(v,str)]])
    point_breeds=_breeds(scope_text,dictionary)
    candidates=point_breeds or question_breeds
    from services.health_knowledge_import_service import infer_species
    species=infer_species('；'.join(conditions),explicit.get('species'))
    if species is None and len({s for s,b in candidates})==1:species=next(iter(candidates))[0]
    if species is None:species=infer_species(question,explicit.get('species'))
    known_species={r['species_code'] for r in dictionary['species']}
    if species not in known_species:
        species=None;notes.append('物种无法唯一确定，待审核')
    breeds={b for s,b in candidates if s==species}
    raw_breed=explicit.get('breed')
    explicit_known={(r['species_code'],r['breed_code']) for r in dictionary['breeds']}
    if raw_breed in ('all_cat','all_dog','unknown_cat','unknown_dog') and (species,raw_breed) in explicit_known:
        breed=raw_breed
        # Preserve an explicitly named question breed unless the cause declares a broader scope.
        if raw_breed.startswith('unknown_') and len(breeds)==1:breed=next(iter(breeds))
    elif len(breeds)==1:breed=next(iter(breeds))
    elif len(breeds)>1:breed=None;notes.append('涉及多个品种，未自动合并归属')
    elif species:
        breed='unknown_'+species
        if (species,breed) not in explicit_known:breed=None
        if raw_breed and raw_breed not in ('unknown','未明确','未知'):notes.append('品种未匹配标准字典，原值保留')
    else:breed=None
    def detect_stages(text):
        found=set()
        terms=[('all_stages',('全生命阶段','全阶段','全年龄','all_stages')),('neonatal',('新生','neonatal')),
            ('weaning',('断奶','weaning')),('senior',('老年','老龄','senior')),
            ('adult',('成年','成猫','成犬','adult')),('kitten',('幼猫','kitten')),('puppy',('幼犬','puppy'))]
        for code,names in terms:
            if any(_contains(text,t) for t in names):found.add(code)
        if '幼年' in text and species:found.add('kitten' if species=='cat' else 'puppy')
        if not found:
            for char,value in [('一','1'),('二','2'),('三','3'),('四','4'),('五','5'),('六','6'),('七','7'),('八','8'),('九','9')]:
                text=text.replace(char+'岁',value+'岁')
            # Attach the unit to both endpoints so a range crossing stages stays pending.
            text=re.sub(r'(\d+(?:\.\d+)?)\s*[-~至到]\s*(\d+(?:\.\d+)?)\s*(个月|月龄|岁)',r'\1\3、\2\3',text)
            for raw,unit in re.findall(r'(\d+(?:\.\d+)?)\s*(个月|月龄|岁)',text):
                age=float(raw)*(12 if unit=='岁' else 1)
                for row in dictionary['stages']:
                    if row['species_code']!=species or row['stage_code'] not in ('kitten','puppy','adult','senior'):continue
                    low=row.get('age_min_months_inclusive');high=row.get('age_max_months_exclusive')
                    if (low is None or age>=float(low)) and (high is None or age<float(high)):found.add(row['stage_code'])
        return found
    stage_codes={row['stage_code'] for row in dictionary['stages'] if row['species_code']==species}
    # Specific cause conditions take precedence over the generic header default.
    stages=detect_stages('；'.join(conditions))
    stage_value=point.get('life_stage')
    if stage_value not in (None,''):stages=detect_stages(stage_value)
    elif not stages:
        stage_value=doc.get('life_stage')
        stages=detect_stages(stage_value or '')
        question_stages=detect_stages(question)
        if not stages or stages=={'all_stages'} and question_stages:stages=question_stages
    if len(stages)==1 and stages<=stage_codes:stage=next(iter(stages))
    elif stages:stage=None;notes.append('生命阶段多值或与物种不匹配，待审核')
    elif species and stage_value not in (None,'','unknown','unspecified','未说明','未知','阶段未指明'):
        stage=None;notes.append('生命阶段无法匹配标准字典，未扩展为全阶段')
    elif species:stage='all_stages'
    else:stage=None
    return {'species_code':species,'breed_code':breed,'life_stage_code':stage,
            'object_scope':{'status':'resolved' if species and breed and stage else 'pending','notes':notes,
                'raw':explicit,'applicable_conditions':conditions}}
