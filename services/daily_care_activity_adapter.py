"""Adapt V3/V4 care_activities without inventing steps or losing nested knowledge."""
from copy import deepcopy
import json
from urllib.parse import urlparse
from vendor.chongxi_care_import.import_care import parse_document

DOMAIN_CATEGORIES={'feeding_care':'feeding_care','grooming_care':'grooming','preventive_care':'preventive_care',
 'oral_care':'oral_care','environment_care':'environment','behavior_care':'behavior_care',
 'growth_care':'growth_care','outdoor_care':'outdoor_care','safety_care':'safety_care'}


def points_for(document):
    result=[]
    for row in parse_document(document):
        activity=row['original_payload']
        schedule=activity.get('schedule')
        urls=activity.get('source_urls',document.get('source_urls',[]))
        if not isinstance(urls,list):raise ValueError('source_urls必须是数组')
        if any(not isinstance(url,str) or urlparse(url).scheme not in {'http','https'} or not urlparse(url).netloc for url in urls):
            raise ValueError('来源须为HTTP/HTTPS网址')
        result.append({
            'record_type':'care_activity','species':row['species'],'breed':row['breed'],'life_stage':row['life_stage'],
            'care_code':row['care_activity_code'],'care_name':row['care_activity_name'],
            'need_code':row['need_code'],'care_subtype':row['care_subtype'],
            'category_code':DOMAIN_CATEGORIES.get(row['care_domain'],'other'),
            'applicable_conditions':deepcopy(activity.get('applicable_conditions')),
            'frequency':json.dumps(schedule,ensure_ascii=False) if schedule is not None else '原文未提供周期',
            'steps':deepcopy(activity.get('methods') or []),'observation_fields':[],
            'precautions':deepcopy(activity.get('avoidances') or []),
            'stop_conditions':deepcopy(activity.get('adjustment_rules') or []),
            'source_urls':deepcopy(urls),'evidence_status':row['evidence_status'],
            'care_activity':deepcopy(activity),'source_scope':deepcopy(row['source_scope']),
        })
    return result


def dictionary_scope(point):
    # Map only explicit aliases; keep all original labels in point_data/summary_meta.
    scope={k:point[k] for k in ('species','breed','life_stage')}
    species={'felis_catus':'cat','canis_lupus_familiaris':'dog','猫':'cat','犬':'dog','狗':'dog'}.get(scope['species'],scope['species'])
    scope['species']=species
    if scope['breed']=='all' and species in {'cat','dog'}:scope['breed']='all_'+species
    scope['life_stage']={'all':'all_stages','adult_dog':'adult','senior_dog':'senior'}.get(scope['life_stage'],scope['life_stage'])
    return scope
