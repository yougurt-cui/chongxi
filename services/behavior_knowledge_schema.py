"""Health-compatible names and order for behavior knowledge dimensions."""
FIELDS = {
    'id': 'BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY',
    'species_code': 'VARCHAR(32) NULL',
    'breed_code': 'VARCHAR(255) NULL',
    'life_stage_code': 'VARCHAR(32) NULL',
    'observation_code': "VARCHAR(160) GENERATED ALWAYS AS (CASE WHEN JSON_TYPE(JSON_EXTRACT(point_data,'$.observation.observation_code'))='STRING' THEN JSON_UNQUOTE(JSON_EXTRACT(point_data,'$.observation.observation_code')) ELSE NULL END) STORED",
    'observation_name': "VARCHAR(160) GENERATED ALWAYS AS (CASE WHEN JSON_TYPE(JSON_EXTRACT(point_data,'$.observation.observation_name'))='STRING' THEN JSON_UNQUOTE(JSON_EXTRACT(point_data,'$.observation.observation_name')) ELSE NULL END) STORED",
    'category_code': 'VARCHAR(32) NOT NULL',
    'category_name': 'VARCHAR(255) NULL',
    **{name: f"JSON GENERATED ALWAYS AS (CASE WHEN JSON_TYPE(JSON_EXTRACT(point_data,'$.observation.{name}'))='ARRAY' THEN JSON_EXTRACT(point_data,'$.observation.{name}') ELSE NULL END) STORED"
       for name in ('normal_scenarios','abnormal_scenarios','unmapped_health_issues')},
    'evidence_status': "VARCHAR(32) NOT NULL DEFAULT 'candidate'",
    'review_status': "VARCHAR(32) NOT NULL DEFAULT 'pending'",
    'source_count': 'INT UNSIGNED NOT NULL',
    'model_count': 'INT UNSIGNED NOT NULL',
    'sources': 'JSON NOT NULL',
    'source_urls': 'JSON NOT NULL',
    'summary_meta': 'JSON NOT NULL',
    'updated_at': 'DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP',
    'batch_id': 'BIGINT UNSIGNED NOT NULL',
    'source_call_id': 'BIGINT UNSIGNED NULL',
    'point_key': 'CHAR(64) NOT NULL',
    'source_hash': 'CHAR(64) NULL',
    'parser_version': 'VARCHAR(64) NULL',
    'behavior_name': 'VARCHAR(160) NOT NULL',
    'point_data': 'JSON NOT NULL',
    'created_at': 'DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP',
}
JSON_FIELDS = {'sources','source_urls','summary_meta','point_data'}
GENERATED_FIELDS = {'observation_code','observation_name','normal_scenarios','abnormal_scenarios','unmapped_health_issues'}
WRITE_FIELDS = ['species_code','breed_code','life_stage_code','category_code','category_name',
                'evidence_status','source_count','model_count','sources','source_urls','summary_meta']


def create_ddl(table='behavior_knowledge_point_detail'):
    import re
    if not re.fullmatch(r'behavior_knowledge_point_detail(?:_[a-zA-Z0-9_]+)?',table):
        raise ValueError('行为知识表名无效')
    columns = ',\n '.join('`'+name+'` '+definition for name,definition in FIELDS.items())
    return f'''CREATE TABLE IF NOT EXISTS `{table}` (
 {columns},
 UNIQUE KEY uk_behavior_source (batch_id,point_key),
 KEY idx_behavior_filters (species_code,category_code),
 KEY idx_behavior_object (species_code,breed_code(96),life_stage_code,observation_code),
 KEY idx_behavior_review (review_status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4'''


def require_aligned(cur):
    cur.execute('SHOW COLUMNS FROM behavior_knowledge_point_detail')
    names = [row['Field'] for row in cur.fetchall()]
    if names != list(FIELDS):
        raise RuntimeError('行为知识表维度尚未对齐，请执行 scripts/align_behavior_knowledge_dimensions.py --apply')
