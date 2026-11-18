"""Behavior knowledge candidates with source provenance and retry-safe imports."""
import hashlib
from urllib.parse import urlparse
from services import model_knowledge_import_service as store
from services.solution_knowledge_service import text, strings

CATEGORIES = {
    'elimination': '排泄行为', 'aggression': '攻击与冲突', 'anxiety': '恐惧与分离焦虑',
    'vocalization': '叫声与表达', 'scratching': '抓挠与啃咬', 'social': '社交与适应',
    'play': '游戏与活动', 'feeding': '进食行为', 'repetitive': '重复与异常行为',
    'training': '训练与习惯', 'other': '其他行为',
}
DDL = '''CREATE TABLE IF NOT EXISTS behavior_knowledge_point_detail (
 id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
 batch_id BIGINT UNSIGNED NOT NULL,
 point_key CHAR(64) NOT NULL,
 behavior_name VARCHAR(160) NOT NULL,
 species VARCHAR(16) NOT NULL,
 category VARCHAR(32) NOT NULL,
 point_data JSON NOT NULL,
 review_status ENUM('pending','verified','rejected') NOT NULL DEFAULT 'pending',
 created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE KEY uk_behavior_source (batch_id,point_key),
 KEY idx_behavior_filters (species,category), KEY idx_behavior_review (review_status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4'''


def validate_document(value):
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


def import_batch(batch_id):
    store.ensure_database()
    with store.connect() as conn:
        try:
            with conn.cursor() as cur:
                # The batch row serializes retries, including concurrent manual imports.
                cur.execute('SELECT * FROM knowledge_import_batch WHERE id=%s FOR UPDATE', (batch_id,))
                batch = cur.fetchone()
                if not batch:
                    raise LookupError('采集批次不存在')
                if batch['knowledge_base'] != 'behavior' or batch['status'] != 'success':
                    raise ValueError('只能导入成功的行为知识批次')
                doc = validate_document(store.decode(batch['parsed_response']))
                points = {hashlib.sha256(store.dumps(p).encode()).hexdigest(): p for p in doc['behaviors']}
                cur.execute('SELECT point_key,review_status FROM behavior_knowledge_point_detail WHERE batch_id=%s', (batch_id,))
                existing = list(cur.fetchall()); old = {p['point_key'] for p in existing}
                if old != set(points) and any(p['review_status'] != 'pending' for p in existing):
                    raise ValueError('行为知识发生变化且已有人工审核记录，请人工处理')
                for key in old - set(points):
                    cur.execute('DELETE FROM behavior_knowledge_point_detail WHERE batch_id=%s AND point_key=%s', (batch_id, key))
                for key in set(points) - old:
                    p = points[key]
                    cur.execute('''INSERT INTO behavior_knowledge_point_detail
                        (batch_id,point_key,behavior_name,species,category,point_data)
                        VALUES (%s,%s,%s,%s,%s,%s)''',
                        (batch_id, key, p['behavior_name'], p['species'], p['category'], store.dumps(p)))
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
    for column, value in (('p.species', species), ('p.category', category), ('b.task_id', task_id)):
        if value is not None and value != '':
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
        row['point_data'] = store.decode(row['point_data'])
    return {'items': rows, 'total': total, 'page': page, 'page_size': page_size, 'categories': CATEGORIES}
