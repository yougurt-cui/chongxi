"""Dictionary-bound dimension naming shared with the health knowledge library."""
import json
from urllib.parse import urlparse
from services.health_object_scope_service import load_dictionary

VERSION = 'behavior-health-dimensions-v1'
UNKNOWN = {'','unknown','unspecified','未限定','未提供','未明确','未知','阶段未指明','未说明','none','null'}


def _key(value):
    return str(value or '').strip().casefold()


def normalize_scope(point, dictionary):
    raw = {k:point.get(k) for k in ('species','breed','life_stage')}
    notes=[]
    species_aliases={'cat':'cat','猫':'cat','猫咪':'cat','felis_catus':'cat',
                     'dog':'dog','狗':'dog','犬':'dog','canis_lupus_familiaris':'dog'}
    sp=species_aliases.get(_key(raw['species']))
    if sp not in {r['species_code'] for r in dictionary['species']}:sp=None
    breed=None;stage=None
    if sp:
        raw_breed=_key(raw['breed'])
        if raw_breed in UNKNOWN:
            breed='unknown_'+sp
        else:
            matches={r['breed_code'] for r in dictionary['breeds'] if r['species_code']==sp and
                     raw_breed in {_key(v) for v in (r['breed_code'],r['breed_name'],*r.get('aliases_pipe','').split('|'))}}
            if len(matches)==1:breed=next(iter(matches))
            else:notes.append('品种无法唯一匹配字典，保留原值待审核')
        if breed not in {r['breed_code'] for r in dictionary['breeds'] if r['species_code']==sp}:breed=None
        raw_stage=_key(raw['life_stage'])
        aliases={'成年':'adult','成猫':'adult','成犬':'adult','老年':'senior','老龄':'senior',
                 '幼猫':'kitten','幼犬':'puppy','幼年':'kitten' if sp=='cat' else 'puppy',
                 '新生期':'neonatal','断奶过渡期':'weaning','全生命阶段':'all_stages','全阶段':'all_stages','全年龄':'all_stages'}
        if raw_stage in UNKNOWN:
            stage='unspecified';notes.append('阶段未指明，不等同全生命阶段')
        else:
            stage=aliases.get(raw_stage,raw_stage)
        if stage not in {r['stage_code'] for r in dictionary['stages'] if r['species_code']==sp}:
            stage=None;notes.append('生命阶段未匹配物种字典，保留原值待审核')
    else:notes.append('物种无法唯一匹配字典，维度保留NULL')
    return {'species_code':sp,'breed_code':breed,'life_stage_code':stage,
            'summary_meta':{'normalization_version':VERSION,'raw_scope':raw,'notes':notes,
                            'scope_status':'resolved' if sp and breed and stage and stage!='unspecified' else 'pending'}}


def values_for(point, batch, dictionary, *, call_id=None, source_hash=None, parser_version=None):
    from services.behavior_knowledge_service import CATEGORIES
    result=normalize_scope(point,dictionary)
    urls=set()
    for url in point.get('source_urls',[]):
        if isinstance(url,str) and urlparse(url).scheme in {'http','https'} and urlparse(url).netloc:urls.add(url)
    search=batch.get('search_metadata')
    if isinstance(search,str):search=json.loads(search)
    for source in (search or {}).get('sources',[]):
        url=source.get('url') if isinstance(source,dict) else None
        if isinstance(url,str) and urlparse(url).scheme in {'http','https'} and urlparse(url).netloc:urls.add(url)
    result.update(category_code=point['category'],category_name=CATEGORIES[point['category']],
        evidence_status='candidate',source_count=1,model_count=0 if batch.get('provider')=='file' else 1,
        sources=[{'batch_id':batch['id'],'source_call_id':call_id,'source_provider':batch['provider'],
                  'source_model':batch['model'],'source_hash':source_hash,'parser_version':parser_version}],
        source_urls=sorted(urls))
    result['summary_meta'].update(raw_evidence_status=point.get('evidence_status'),
        grain='one model-source knowledge point; not a cross-model summary')
    return result
