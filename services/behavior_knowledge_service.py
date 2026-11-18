"""Behavior knowledge candidates with source provenance and retry-safe imports."""
import hashlib
import copy
from urllib.parse import urlparse
from services import model_knowledge_import_service as store
from services.solution_knowledge_service import text, strings

CATEGORIES = {
    'elimination': '排泄行为', 'aggression': '攻击与冲突', 'anxiety': '恐惧与分离焦虑',
    'vocalization': '叫声与表达', 'scratching': '抓挠与啃咬', 'social': '社交与适应',
    'play': '游戏与活动', 'feeding': '进食行为', 'repetitive': '重复与异常行为',
    'training': '训练与习惯', 'other': '其他行为',
}
from services.behavior_knowledge_schema import create_ddl, WRITE_FIELDS, JSON_FIELDS
DDL = create_ddl()


def validate_document(value):
    if isinstance(value, dict) and 'observations' in value:
        return validate_observations(value)
    if not isinstance(value, dict) or value.get('knowledge_base') != 'behavior':
        raise ValueError('行为知识文档必须指定 knowledge_base=behavior')
    question = text(value.get('question'), 'question', 2000)
    items = value.get('behaviors')
    if not isinstance(items, list) or not 1 <= len(items) <= 30:
        raise ValueError('behaviors必须是1至30条行为知识的数组')
    points = []; seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('行为知识必须为对象')
        if item.get('record_type') == 'observation':
            normalized = validate_observations({'question': question, 'observations': [item['observation']],
                'species': item['species'], 'breed': item['breed'], 'life_stage': item['life_stage']})
            points.extend(normalized['behaviors'])
            continue
        point = {k: text(item.get(k), k, limit) for k, limit in
                 (('behavior_name', 160), ('breed', 120), ('life_stage', 120))}
        if item.get('species') not in {'cat', 'dog', 'unknown'}:
            raise ValueError('species必须为cat/dog/unknown')
        if item.get('category') not in CATEGORIES:
            raise ValueError('行为分类无效')
        point.update(species=item['species'], category=item['category'])
        for key in ('manifestations', 'possible_causes', 'assessment', 'observation_fields',
                    'stop_conditions', 'source_urls'):
            point[key] = strings(item.get(key), key)
        if not point['manifestations'] or not point['assessment']:
            raise ValueError('必须提供行为表现与判断所需信息')
        for url in point['source_urls']:
            parsed = urlparse(url)
            if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
                raise ValueError('来源链接必须是HTTP或HTTPS网址')
        steps = item.get('interventions')
        if not isinstance(steps, list) or not 1 <= len(steps) <= 15:
            raise ValueError('interventions必须为1至15条步骤')
        point['interventions'] = []
        for step in steps:
            if not isinstance(step, dict):
                raise ValueError('干预步骤必须包含action和reason')
            point['interventions'].append({k: text(step.get(k), k, 1500) for k in ('action', 'reason')})
        evidence = item.get('evidence_status')
        if evidence not in {'supported', 'uncertain', 'pending_verification'}:
            raise ValueError('行为知识证据状态无效')
        point['evidence_status'] = evidence
        key = hashlib.sha256(store.dumps(point).encode()).hexdigest()
        if key not in seen:
            points.append(point); seen.add(key)
    return {'knowledge_base': 'behavior', 'question': question, 'behaviors': points}


def validate_observations(value):
    """Preserve the observation contract; never invent training steps or disease links."""
    if value.get('knowledge_base', 'behavior') != 'behavior':
        raise ValueError('观察现象文档知识库不匹配')
    question = text(value.get('question'), 'question', 2000)
    observations = value.get('observations')
    if not isinstance(observations, list) or not 1 <= len(observations) <= 30:
        raise ValueError('observations必须为1至30条观察现象的数组')
    species = {'cat':'cat', '猫':'cat', '猫咪':'cat', 'felis_catus':'cat',
               'dog':'dog', '狗':'dog', '犬':'dog', 'canis_lupus_familiaris':'dog'}.get(value.get('species'), 'unknown')
    points = []; seen = set()
    for original in observations:
        if not isinstance(original, dict):
            raise ValueError('观察现象必须为对象')
        item = copy.deepcopy(original)
        for key in ('observation_code', 'observation_name', 'domain_code', 'category_code', 'observation_type'):
            text(item.get(key), key, 160)
        for key in ('aliases', 'observable_features'):
            strings(item.get(key), key, limit=50)
        related = item.get('related_observations')
        if not isinstance(related, list) or len(related) > 50:
            raise ValueError('related_observations必须为数组且最多50条')
        for relation in related:
            if isinstance(relation, str):
                text(relation, 'related_observations', 800)
            elif isinstance(relation, dict):
                text(relation.get('observation_name'), 'related observation_name', 160)
                for key, val in relation.items():
                    text(val, 'related_observations.' + key, 2000)
            else:
                raise ValueError('related_observations条目必须为编码或观察现象对象')
        for key in ('normal_scenarios', 'abnormal_scenarios', 'unmapped_health_issues'):
            if not isinstance(item.get(key), list) or len(item[key]) > 50:
                raise ValueError(key + '必须是数组且最多50条')
        for scene in item['normal_scenarios'] + item['abnormal_scenarios']:
            if not isinstance(scene, dict):
                raise ValueError('场景必须为对象')
            text(scene.get('scenario'), 'scenario', 2000)
            strings(scene.get('conditions'), 'conditions', limit=50)
            strings(scene.get('source_ids', []), 'source_ids', limit=50)
        for scene in item['normal_scenarios']:
            text(scene.get('mechanism'), 'mechanism', 6000)
            strings(scene.get('observable_signs'), 'observable_signs', limit=50)
        for scene in item['abnormal_scenarios']:
            text(scene.get('boundary_id'), 'boundary_id', 160)
            strings(scene.get('warning_signs'), 'warning_signs', limit=50)
            if scene.get('risk_level') not in {'routine', 'veterinary_review', 'urgent'}:
                raise ValueError('异常边界risk_level无效')
            relations = scene.get('health_relations')
            if not isinstance(relations, list) or len(relations) > 50:
                raise ValueError('health_relations必须为数组且最多50条')
            for relation in relations:
                if not isinstance(relation, dict):
                    raise ValueError('健康候选关联必须为对象')
                text(relation.get('health_issue_name'), 'health_issue_name', 500)
                if relation.get('relation_type') not in {'possible_manifestation', 'supporting_sign', 'nonspecific_sign'}:
                    raise ValueError('健康候选关联relation_type无效')
                text(relation.get('mechanism_summary'), 'mechanism_summary', 6000)
                for key in ('supporting_signs', 'differentiating_signs', 'verification'):
                    strings(relation.get(key), key, limit=50)
                ids = relation.get('source_knowledge_ids', [])
                if not isinstance(ids, list) or any(isinstance(v, bool) or not isinstance(v, (str, int)) for v in ids):
                    raise ValueError('source_knowledge_ids必须为编码数组')
        for issue in item['unmapped_health_issues']:
            if not isinstance(issue, (str, dict)):
                raise ValueError('未映射健康问题必须为文字或对象')
        if item.get('evidence_status') not in {'pending_review', 'pending_verification', 'supported', 'uncertain', 'unknown'}:
            raise ValueError('观察现象evidence_status无效')
        point = {'record_type': 'observation', 'behavior_name': item['observation_name'],
                 'species': species, 'breed': value.get('breed') or '未限定',
                 'life_stage': value.get('life_stage') or '未限定', 'category': 'other',
                 'evidence_status': 'pending_verification', 'observation': item}
        for key in ('breed', 'life_stage'):
            text(point[key], key, 120)
        digest = hashlib.sha256(store.dumps(point).encode()).hexdigest()
        if digest not in seen:
            points.append(point); seen.add(digest)
    return {'knowledge_base': 'behavior', 'question': question, 'behaviors': points}


def render_prompt(template, question):
    defaults = {'question': question, 'species': '未提供；仅依据问题中的明确物种信息',
                'breed': '未提供', 'life_stage': '未提供', 'existing_observations': '[]',
                'disease_candidates': '[]', 'reference_materials': '未提供本地参考资料；联网来源仍需核验'}
    # Do not interpret placeholder-like text supplied inside the user's question.
    import re
    return re.sub(r'\{\{(question|species|breed|life_stage|existing_observations|disease_candidates|reference_materials)\}\}',
                  lambda match: defaults[match.group(1)], template)


def import_batch(batch_id):
    from services.behavior_model_pipeline import parse_batch
    return parse_batch(batch_id)


def _import_points(batch_id, document, *, call_id=None, source_hash=None, parser_version=None):
    store.ensure_database()
    from services.behavior_knowledge_dimensions import load_dictionary, values_for
    dictionary = load_dictionary()
    with store.connect() as conn:
        try:
            with conn.cursor() as cur:
                # The batch row serializes retries, including concurrent manual imports.
                cur.execute('SELECT * FROM knowledge_import_batch WHERE id=%s FOR UPDATE', (batch_id,))
                batch = cur.fetchone()
                if not batch:
                    raise LookupError('采集批次不存在')
                if batch['knowledge_base'] != 'behavior' or batch['status'] not in {'success', 'parse_failed'}:
                    raise ValueError('只能导入成功的行为知识批次')
                doc = validate_document(document)
                points = {hashlib.sha256(store.dumps(p).encode()).hexdigest(): p for p in doc['behaviors']}
                cur.execute('SELECT point_key,review_status FROM behavior_knowledge_point_detail WHERE batch_id=%s', (batch_id,))
                existing = list(cur.fetchall()); old = {p['point_key'] for p in existing}
                if old != set(points) and any(p['review_status'] != 'pending' for p in existing):
                    raise ValueError('行为知识发生变化且已有人工审核记录，请人工处理')
                for key in old - set(points):
                    cur.execute('DELETE FROM behavior_knowledge_point_detail WHERE batch_id=%s AND point_key=%s', (batch_id, key))
                for key, p in points.items():
                    dims = values_for(p, batch, dictionary, call_id=call_id,
                                      source_hash=source_hash, parser_version=parser_version)
                    values = tuple(store.dumps(dims[k]) if k in JSON_FIELDS else dims[k] for k in WRITE_FIELDS)
                    if key not in old:
                        columns = ('batch_id','point_key','behavior_name',*WRITE_FIELDS,'point_data')
                        cur.execute('INSERT INTO behavior_knowledge_point_detail ('+','.join(columns)+') VALUES ('+
                                    ','.join(['%s']*len(columns))+')',
                                    (batch_id,key,p['behavior_name'],*values,store.dumps(p)))
                    else:
                        cur.execute('UPDATE behavior_knowledge_point_detail SET '+','.join(k+'=%s' for k in WRITE_FIELDS)+
                                    ' WHERE batch_id=%s AND point_key=%s', (*values,batch_id,key))
                if call_id is not None:
                    cur.execute('''UPDATE behavior_knowledge_point_detail
                        SET source_call_id=%s,source_hash=%s,parser_version=%s WHERE batch_id=%s''',
                        (call_id, source_hash, parser_version, batch_id))
                cur.execute('UPDATE knowledge_import_batch SET imported_at=COALESCE(imported_at,NOW()),import_error=NULL WHERE id=%s', (batch_id,))
            conn.commit()
        except Exception:
            conn.rollback(); raise
    return {'batch_id': batch_id, 'status': 'already_imported' if old == set(points) else 'completed',
            'total_count': len(points), 'inserted_count': len(set(points) - old)}


def list_points(*, page=1, query='', species='', category='', task_id=None):
    try:
        page = int(page)
    except (ValueError, TypeError):
        raise ValueError('页码不正确')
    if not 1 <= page <= 100000 or len(query) > 200:
        raise ValueError('页码或搜索词长度无效')
    if species and species not in {'cat', 'dog', 'unknown'}:
        raise ValueError('物种无效')
    if category and category not in CATEGORIES:
        raise ValueError('行为分类无效')
    store.ensure_database()
    clauses = ["b.knowledge_base='behavior'", "b.status='success'"]; params = []
    for column, value in (('p.species_code', species), ('p.category_code', category), ('b.task_id', task_id)):
        if column == 'p.species_code' and value == 'unknown':
            clauses.append('p.species_code IS NULL')
        elif value is not None and value != '':
            clauses.append(column + '=%s'); params.append(value)
    if query:
        clauses.append("(p.behavior_name LIKE %s OR CAST(p.point_data AS CHAR) LIKE %s)")
        # SQL parameters prevent injection; wildcard characters search literally.
        escaped = query.replace('!', '!!').replace('%', '!%').replace('_', '!_')
        clauses[-1] = "(p.behavior_name LIKE %s ESCAPE '!' OR CAST(p.point_data AS CHAR) LIKE %s ESCAPE '!')"
        params.extend(['%' + escaped + '%'] * 2)
    join = ' FROM behavior_knowledge_point_detail p JOIN knowledge_import_batch b ON b.id=p.batch_id WHERE ' + ' AND '.join(clauses)
    page_size = 100 if task_id is not None else 20
    with store.connect() as conn, conn.cursor() as cur:
        cur.execute('SELECT COUNT(*) n' + join, params); total = cur.fetchone()['n']
        cur.execute('''SELECT p.*,b.task_id,b.provider source_provider,b.model source_model,
            b.question source_question''' + join + ' ORDER BY p.id DESC LIMIT %s OFFSET %s', params + [page_size, (page - 1) * page_size])
        rows = list(cur.fetchall())
    for row in rows:
        for key in ('point_data','sources','source_urls','summary_meta'):
            row[key] = store.decode(row.get(key))
    return {'items': rows, 'total': total, 'page': page, 'page_size': page_size, 'categories': CATEGORIES}
