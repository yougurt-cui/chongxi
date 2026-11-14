"""Canonical needs and question-level scenarios shared by every provider."""
import re,json,hashlib
from datetime import datetime,timezone
from services import model_knowledge_import_service as store
VERSION='2026-11-11-need-scene-v1'
# These are spelling/terminology aliases, not inferred diagnoses.
ALIASES={'soft_stools':'soft_stool','food_change':'food_transition','diet_transition':'food_transition',
 'food_intolerance':'food_allergy','product_palatability':'product_palability',
 'sensitive_stomach':'digestive_sensitivity','digestive_sensitive':'digestive_sensitivity',
 'gastrointestinal_sensitivity':'digestive_sensitivity','stomach_sensitivity':'digestive_sensitivity'}
SENSITIVE_ALIASES=('肠胃敏感','胃肠敏感','敏感肠胃','消化敏感','消化道敏感','胃肠道敏感')


def normalized(value):return re.sub(r'[\s_-]+','_',value.strip().casefold())


def alias_map(dictionary):
    result={}
    for row in dictionary:
        for term in [row['need_code'],row.get('need_name') or row['need_code'],*(row.get('aliases_pipe') or '').split('|')]:
            if term:result.setdefault(normalized(term),set()).add(row['need_code'])
    known={r['need_code'] for r in dictionary}
    for term,code in ALIASES.items():
        if code in known:result.setdefault(normalized(term),set()).add(code)
    return result


def normalize_codes(codes,dictionary):
    if not isinstance(codes,list) or any(not isinstance(c,str) for c in codes):raise ValueError('需求编码必须为字符串数组')
    aliases=alias_map(dictionary);canonical=[];unknown=[]
    for value in codes:
        found=aliases.get(normalized(value),set())
        if len(found)==1:canonical.append(next(iter(found)))
        else:unknown.append(value)
    return sorted(set(canonical)),sorted(set(unknown))


def _contains(text,term):
    if not term:return False
    return bool(re.search(r'(?<![a-z0-9_])'+re.escape(term)+r'(?![a-z0-9_])',text,re.I)) if term.isascii() else term in text


def scene_for_question(question,dictionary):
    """Question is the submitted batch question, never a provider's echoed label."""
    known={r['need_code']:r for r in dictionary};codes=set()
    for row in dictionary:
        if any(_contains(question,t) for t in [row['need_name'],*(row.get('aliases_pipe') or '').split('|'),row['need_code']]):codes.add(row['need_code'])
    if any(t in question for t in SENSITIVE_ALIASES) and 'digestive_sensitivity' in known:codes.add('digestive_sensitivity')
    if any(t in question for t in ('怎么选粮','如何选粮','推荐吃什么','吃什么粮','选什么粮','推荐猫粮','推荐狗粮')):codes.add('food_selection')
    generic={'food_selection','product_recommendation','product_analysis'}
    specific=codes-generic
    if specific:codes=specific
    elif 'food_selection' in codes:codes={'food_selection'}
    codes=sorted(codes)
    relation='or' if len(codes)>1 and re.search(r'或者|或',question) else 'and' if len(codes)>1 else 'single' if codes else 'unspecified'
    if len(codes)==1:code=codes[0]
    elif codes:code=relation+'_'+hashlib.sha256(store.dumps(codes).encode()).hexdigest()[:24]
    else:code='unspecified'
    name=(' 或 ' if relation=='or' else ' + ').join(known[c]['need_name'] for c in codes) if codes else '未指定需求场景'
    return {'need_scene_code':code,'need_scene_name':name,'need_scene_codes':codes,'need_relation':relation,
        'scene_meta':{'version':VERSION,'source':'submitted_question','question':question,
                      'status':'resolved' if codes else 'unspecified'}}


def clean_tags(row,dictionary):
    original=store.decode(row.get('need_codes')) or []
    codes,unknown=normalize_codes(original,dictionary)
    meta=store.decode(row.get('standardization_meta')) or {}
    raw=meta.get('raw_input',{})
    # Only literal evidence counts: sensitivity is not itself soft stool or allergy.
    text='；'.join([row.get('source_question',''),row.get('content',''),raw.get('topic',''),*raw.get('applicable_conditions',[])])
    if any(t in text for t in SENSITIVE_ALIASES):
        if any(r['need_code']=='digestive_sensitivity' for r in dictionary):codes.append('digestive_sensitivity')
        if 'soft_stool' in codes and not any(t in text for t in ('软便','便便软','不成形','不成型','糊便','soft stool')):codes.remove('soft_stool')
        if 'food_allergy' in codes and not any(t in text for t in ('食物过敏','食物不耐受','食物不良反应','排除饮食','水解','低敏','过敏','不耐受')):codes.remove('food_allergy')
    return sorted(set(codes)),unknown


def load_dictionary():
    import pymysql
    from app_config import get_mysql_config
    cfg=get_mysql_config(database='chongxi_knowledge_dictionary');cfg['cursorclass']=pymysql.cursors.DictCursor
    with pymysql.connect(**cfg) as conn,conn.cursor() as cur:
        cur.execute('SELECT * FROM dict_user_need ORDER BY need_code');return list(cur.fetchall())


def sync_scene_fields(*,dictionary=None,dictionary_hash=None):
    dictionary=dictionary or load_dictionary();changed=0;tag_changed=0;unknown_rows=[]
    with store.connect() as conn,conn.cursor() as cur:
        cur.execute("SELECT d.*,b.question source_question FROM knowledge_point_source_detail d JOIN knowledge_import_batch b ON b.id=d.batch_id WHERE b.knowledge_base='selection' ORDER BY d.id")
        rows=list(cur.fetchall())
        for row in rows:
            scene=scene_for_question(row['source_question'],dictionary)
            codes,unknown=clean_tags(row,dictionary)
            if unknown:unknown_rows.append({'id':row['id'],'unknown':unknown})
            meta=store.decode(row.get('standardization_meta')) or {}
            old_codes=store.decode(row['need_codes']) or []
            if codes!=old_codes or unknown:
                audit=meta.get('enum_cleaning',{})
                if not audit:audit={'previous_need_codes':old_codes,'previous_dictionary_hash':meta.get('dictionary_hash')}
                audit.update(version=VERSION,unknown_values=unknown,processed_at=datetime.now(timezone.utc).isoformat())
                meta['enum_cleaning']=audit;tag_changed+=1
            if dictionary_hash and meta.get('status')=='success':
                if meta.get('dictionary_hash')!=dictionary_hash:
                    meta.setdefault('enum_cleaning',{}).setdefault('previous_dictionary_hash',meta.get('dictionary_hash'))
                    meta['enum_cleaning'].update(version=VERSION,dictionary_update='append-only; existing codes revalidated')
                meta['dictionary_hash']=dictionary_hash
            values=(scene['need_scene_code'],scene['need_scene_name'],store.dumps(scene['need_scene_codes']),scene['need_relation'],store.dumps(scene['scene_meta']),store.dumps(codes),store.dumps(meta))
            cur.execute('UPDATE knowledge_point_source_detail SET need_scene_code=%s,need_scene_name=%s,need_scene_codes=%s,need_relation=%s,scene_meta=%s,need_codes=%s,standardization_meta=%s WHERE id=%s',values+(row['id'],));changed+=cur.rowcount
        conn.commit()
    return {'rows':len(rows),'changed':changed,'tag_changed':tag_changed,'unknown_rows':unknown_rows}
