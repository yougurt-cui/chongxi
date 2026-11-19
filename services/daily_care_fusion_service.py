"""Deterministic, provenance-preserving care fusion; never medical adjudication."""
import hashlib
import json

TABLE='daily_care_knowledge_point'
LINK_TABLE='daily_care_knowledge_point_source'

def stable(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
def digest(value):return hashlib.sha256(stable(value).encode()).hexdigest()


VALUE_FIELDS=('care_activity_name','care_category','care_objective','applicable_conditions','schedule','tools_materials',
              'methods','avoidances','observations','adjustment_rules','source_urls','source_ids',
              'evidence_status','extraction_notes')
JSON_FIELDS=(*VALUE_FIELDS,'raw_scope','source_providers','source_models','source_task_ids','field_differences')


def ensure_schema(cur):
    cur.execute('''CREATE TABLE IF NOT EXISTS daily_care_knowledge_point (
      id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY, fusion_key CHAR(64) NOT NULL,
      source_question TEXT NOT NULL, species_code VARCHAR(32) NULL, breed_code VARCHAR(255) NULL,
      life_stage_code VARCHAR(32) NULL, raw_scope JSON NOT NULL,
      need_code VARCHAR(100) NULL,care_activity_code VARCHAR(160) NOT NULL,care_subtype VARCHAR(64) NULL,
      care_domain VARCHAR(64) NULL,care_type VARCHAR(32) NULL,
      '''+','.join('`'+k+'` JSON NOT NULL' for k in VALUE_FIELDS)+''',
      source_count INT UNSIGNED NOT NULL,model_count INT UNSIGNED NOT NULL,
      source_providers JSON NOT NULL,source_models JSON NOT NULL,source_task_ids JSON NOT NULL,
      has_differences BOOLEAN NOT NULL,field_differences JSON NOT NULL,
      fusion_method VARCHAR(64) NOT NULL DEFAULT 'exact_context_variants_v1',
      review_status ENUM('pending','verified','rejected') NOT NULL DEFAULT 'pending',
      content_hash CHAR(64) NOT NULL, active BOOLEAN NOT NULL DEFAULT 1,
      created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
      UNIQUE KEY uk_care_fusion(fusion_key),KEY idx_care_fusion_scope(active,species_code,care_domain)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''')
    cur.execute('''CREATE TABLE IF NOT EXISTS daily_care_knowledge_point_source (
      knowledge_point_id BIGINT UNSIGNED NOT NULL,source_detail_id BIGINT UNSIGNED NOT NULL,
      PRIMARY KEY(knowledge_point_id,source_detail_id),UNIQUE KEY uk_care_fusion_source(source_detail_id),
      CONSTRAINT fk_care_fusion_point FOREIGN KEY(knowledge_point_id)
        REFERENCES daily_care_knowledge_point(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''')


def rebuild(cur):
    from services.daily_care_knowledge_service import decode_row
    cur.execute('''SELECT p.*,b.question source_question,b.provider source_provider,
      b.model source_model,b.task_id FROM daily_care_knowledge_point_detail p
      JOIN knowledge_import_batch b ON b.id=p.batch_id
      WHERE b.knowledge_base='daily_care' AND b.status='success' ORDER BY p.id''')
    groups=build_groups([decode_row(r) for r in cur.fetchall()])
    cur.execute('SELECT id,fusion_key,content_hash,active FROM daily_care_knowledge_point FOR UPDATE')
    existing={r['fusion_key']:r for r in cur.fetchall()}
    active_keys={p['fusion_key'] for p in groups}
    for key,old in existing.items():
        if key not in active_keys:
            cur.execute('UPDATE daily_care_knowledge_point SET active=0 WHERE id=%s',(old['id'],))
    # Rebuild links in the same transaction, including removed/rejected sources.
    cur.execute('DELETE FROM daily_care_knowledge_point_source')
    for point in groups:
        old=existing.get(point['fusion_key'])
        source_ids=point.pop('_source_ids')
        columns=list(point);values=[stable(point[k]) if k in JSON_FIELDS else point[k] for k in columns]
        if old:
            point_id=old['id']
            if old['content_hash']!=point['content_hash'] or not old['active']:
                cur.execute('UPDATE daily_care_knowledge_point SET '+','.join('`'+k+'`=%s' for k in columns)+",active=1,review_status='pending' WHERE id=%s",values+[point_id])
        else:
            cur.execute('INSERT INTO daily_care_knowledge_point ('+','.join('`'+k+'`' for k in columns)+') VALUES ('+','.join(['%s']*len(columns))+')',values)
            point_id=cur.lastrowid
        for source_id in source_ids:
            cur.execute('INSERT INTO daily_care_knowledge_point_source (knowledge_point_id,source_detail_id) VALUES (%s,%s)',(point_id,source_id))
    return {'total':len(groups),'source_count':sum(p['source_count'] for p in groups),'model_calls':0}


def acquire_lock(cur):
    from services import model_knowledge_import_service as store
    name='care-fusion:'+digest(store.database_name())[:40]
    cur.execute('SELECT GET_LOCK(%s,30) acquired',(name,))
    if not cur.fetchone()['acquired']:raise ValueError('养护知识正在更新，请稍后重试')
    return name


def build_groups(rows):
    """Consolidate literal field variants; never choose a medical recommendation."""
    buckets={}
    identity=('source_question','species_code','breed_code','life_stage_code','raw_scope',
              'need_code','care_activity_code','care_subtype','care_domain','care_type')
    for row in rows:
        if row['review_status']=='rejected':continue
        context={k:row.get(k) for k in identity}
        key=digest(context)
        buckets.setdefault(key,{'context':context,'rows':[]})['rows'].append(row)
    results=[]
    for key,bucket in sorted(buckets.items()):
        source_rows=sorted(bucket['rows'],key=lambda r:r['id'])
        record={**bucket['context'],'fusion_key':key}
        differences=[]
        for field in VALUE_FIELDS:
            variants={}
            for source in source_rows:
                value=source.get(field)
                token=stable(value)
                variant=variants.setdefault(token,{'value':value,'source_detail_ids':[], 'source_providers':[], 'source_models':[]})
                variant['source_detail_ids'].append(source['id'])
                variant['source_providers'].append(source['source_provider'])
                variant['source_models'].append(source['source_provider']+'/'+source['source_model'])
            values=[]
            for token,variant in sorted(variants.items()):
                variant['source_providers']=sorted(set(variant['source_providers']))
                variant['source_models']=sorted(set(variant['source_models']))
                variant['support_model_count']=len(variant['source_models'])
                values.append(variant)
            record[field]=values
            # Missing values are retained as source gaps, not called contradictions.
            if sum(v['value'] not in (None,[],{},'') for v in values)>1:differences.append(field)
        record.update(source_count=len(source_rows),
            model_count=len({(r['source_provider'],r['source_model']) for r in source_rows}),
            source_providers=sorted({r['source_provider'] for r in source_rows}),
            source_models=sorted({r['source_provider']+'/'+r['source_model'] for r in source_rows}),
            source_task_ids=sorted({r['task_id'] for r in source_rows}),
            has_differences=bool(differences),field_differences=differences,
            fusion_method='exact_context_variants_v1')
        record['content_hash']=digest(record)
        record['_source_ids']=[r['id'] for r in source_rows]
        results.append(record)
    return results
