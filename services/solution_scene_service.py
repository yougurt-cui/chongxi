"""One solution row per species/breed/life-stage/need/scenario, with lossless sources."""
import hashlib
import json
from pathlib import Path

from services import model_knowledge_import_service as store

GRAIN = ('species_code', 'breed_code', 'life_stage_code', 'need_code', 'scenario_code')
REGISTRY = Path(__file__).resolve().parents[1] / 'vendor/chongxi_health_solution/scene_aliases.json'
DDL = '''CREATE TABLE IF NOT EXISTS solution_knowledge_point (
 id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
 species_code VARCHAR(32) NOT NULL,
 breed_code VARCHAR(64) NOT NULL,
 life_stage_code VARCHAR(32) NOT NULL,
 need_code VARCHAR(64) NOT NULL,
 scenario_code VARCHAR(100) NOT NULL,
 scenario TEXT NOT NULL,
 possible_causes JSON NOT NULL,
 steps JSON NOT NULL,
 record_fields JSON NOT NULL,
 verification JSON NOT NULL,
 stop_conditions JSON NOT NULL,
 source_count INT UNSIGNED NOT NULL,
 model_count INT UNSIGNED NOT NULL,
 sources JSON NOT NULL,
 source_urls JSON NOT NULL,
 evidence_status VARCHAR(32) NOT NULL DEFAULT 'pending_verification',
 review_status VARCHAR(32) NOT NULL DEFAULT 'pending',
 input_hash CHAR(64) NOT NULL,
 summary_meta JSON NULL,
 created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
 UNIQUE KEY uk_solution_scene (species_code,breed_code,life_stage_code,need_code,scenario_code),
 KEY idx_solution_scope (species_code,breed_code,life_stage_code,need_code),
 KEY idx_solution_review (review_status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin'''
JSON_FIELDS = ('possible_causes', 'steps', 'record_fields', 'verification', 'stop_conditions', 'sources', 'source_urls', 'summary_meta')


def registry():
    return json.loads(REGISTRY.read_text(encoding='utf-8'))


def identity(point, question='', aliases=None):
    """Only explicit aliases. Never infer a broader scope or fuzzy-merge scenes."""
    aliases = registry() if aliases is None else aliases
    need = aliases.get('need_aliases', {}).get(point['need'], point['need'])
    need = aliases.get('question_needs', {}).get(question.strip(), need)
    scene = aliases.get('scenario_aliases', {}).get(point['scenario_code'], point['scenario_code'])
    stage = {'all': 'all_stages'}.get(point['life_stage'], point['life_stage'])
    return (point['species'], point['breed'], stage, need, scene)


def ensure_schema(cur):
    import pymysql
    try:
        cur.execute("SHOW COLUMNS FROM solution_knowledge_point")
        columns = {r['Field'] for r in cur.fetchall()}
    except pymysql.err.ProgrammingError as exc:
        if exc.args[0] != 1146:
            raise
        columns = set()
    if columns and 'batch_id' in columns:
        raise ValueError('解决方案仍是旧候选表，请先运行 migrate_solution_scene_grain.py 备份迁移')
    cur.execute(DDL)
    if columns and 'summary_meta' not in columns:
        cur.execute('ALTER TABLE solution_knowledge_point ADD COLUMN summary_meta JSON NULL')


def source_groups(rows,*,align=False,use_alignment=False):
    aliases = registry()
    groups = {}
    for row in rows:
        point = store.decode(row.get('scenario_data'))
        if not point:
            continue  # Legacy generic candidates stay in detail; do not invent a scene.
        key = identity(point, row['question'], aliases)
        source = {'candidate_id': row['id'], 'batch_id': row['batch_id'],
                  'task_id': row['task_id'], 'source_provider': row['provider'],
                  'source_model': row['model'], 'source_question': row['question'],
                  'review_status': row['review_status'],
                  'evidence_status': row['evidence_status'],
                  'source_urls': store.decode(row['source_urls']) or [],
                  'scenario_data': point, 'raw_json': store.decode(row['raw_json'])}
        groups.setdefault(key, []).append(source)
    if align or use_alignment:
        from services.solution_scene_alignment_service import regroup
        return regroup(groups,generate=align)
    return groups


def materialize(key, sources):
    # Keep complete variants. The first non-rejected source is a representative,
    # not a consensus or an automatic medical endorsement.
    primary = next((s for s in sources if s['review_status'] != 'rejected'), sources[0])
    point = primary['scenario_data']
    raw = primary['raw_json']
    payload = {'grain': key, 'sources': sources}
    return {**dict(zip(GRAIN, key)), 'scenario': point['scenario'],
            'possible_causes': point['possible_causes'],
            'steps': [{'action': action, 'reason': ''} for action in point['solution']],
            'record_fields': raw.get('record_fields', []),
            'verification': point['verification'],
            'stop_conditions': raw.get('stop_conditions', []),
            'source_count': len(sources),
            'model_count': len({(s['source_provider'], s['source_model']) for s in sources}),
            'sources': sources,
            'source_urls': list(dict.fromkeys(url for s in sources for url in s['source_urls'])),
            'evidence_status': 'pending_verification',
            'input_hash': hashlib.sha256(store.dumps(payload).encode()).hexdigest()}


def refresh(conn, table='solution_knowledge_point', *, rows=None, summarize=True, summarizer=None, keys=None):
    """Called under the solution importer lock, in the same data transaction."""
    if table not in {'solution_knowledge_point', 'solution_knowledge_point_scene_staging'}:
        raise ValueError('Invalid solution summary table')
    with conn.cursor() as cur:
        from services.solution_knowledge_service import source_rows
        groups = source_groups(source_rows(cur) if rows is None else rows,align=summarize)
        cur.execute('SELECT * FROM '+table+' ORDER BY id FOR UPDATE')
        previous = {tuple(r[k] for k in GRAIN): r for r in cur.fetchall()}
        changed = 0
        selected=set(previous) | set(groups)
        if keys is not None:selected &= set(keys)
        for key in sorted(selected):
            old = previous.get(key)
            value = materialize(key, groups[key]) if key in groups else None
            meta=store.decode(old.get('summary_meta')) if old else None
            if value and summarize:
                from services import solution_summary_service as summary
                expected=summary.fingerprint(key,groups[key])
                if old and meta and meta.get('status')=='success' and meta.get('input_hash')==expected:
                    continue
            elif old and value:
                if old['input_hash']==value['input_hash'] or (meta and meta.get('status')=='success' and store.decode(old['sources'])==value['sources']):
                    continue
            if old and old['review_status'] != 'pending':
                raise ValueError('场景方案已有人工审核，来源或内容变化需人工处理')
            if value and summarize:
                model_value,model_meta=(summarizer or summary.summarize)(key,groups[key])
                value.update(summary.materialize(model_value,groups[key]),summary_meta=model_meta,input_hash=expected)
                variants=list(dict.fromkeys(s['scenario_data']['scenario'] for s in groups[key] if s['review_status']!='rejected'))
                if len(variants)>1:
                    value['scenario']='相近观测场景；以下各来源的额外前提分别适用：\n'+'\n\n'.join('【来源场景 '+str(i)+'】'+text for i,text in enumerate(variants,1))
            elif value:
                value['summary_meta']=None
            if value is None:
                cur.execute('DELETE FROM '+table+' WHERE id=%s', (old['id'],))
            else:
                columns = tuple(value)
                values = [store.dumps(value[k]) if k in JSON_FIELDS else value[k] for k in columns]
                if old:
                    cur.execute('UPDATE '+table+' SET '+','.join(k+'=%s' for k in columns)+' WHERE id=%s', (*values, old['id']))
                else:
                    cur.execute('INSERT INTO '+table+' ('+','.join(columns)+') VALUES ('+','.join(['%s']*len(columns))+')', values)
            changed += 1
    return {'scene_count': len(groups), 'changed_count': changed}


def task_points(task_id):
    """Return aggregated scenes touched by this task, including other task sources."""
    with store.connect() as conn, conn.cursor() as cur:
        cur.execute('''SELECT DISTINCT s.* FROM solution_knowledge_point s
            JOIN JSON_TABLE(s.sources, '$[*]' COLUMNS(task_id BIGINT PATH '$.task_id')) refs
              ON refs.task_id=%s ORDER BY s.id''', (task_id,))
        points = list(cur.fetchall())
    for point in points:
        for key in JSON_FIELDS:
            point[key] = store.decode(point[key])
    return points
