import hashlib
import json
from unittest.mock import MagicMock, patch
import pytest
from flask import Flask
from api.enterprise_api import enterprise_api
from services import behavior_knowledge_service as behavior
from services import model_knowledge_import_service as store
from services import multi_model_knowledge_service as multi
from services import multi_model_prompt_service as prompts
from vendor.chongxi_multi_model_mysql import run


def document():
    return {'knowledge_base': 'behavior', 'question': '猫半夜叫如何观察？', 'behaviors': [{
        'behavior_name': '夜间鸣叫', 'species': 'cat', 'breed': '未限定', 'life_stage': '成年',
        'category': 'vocalization', 'manifestations': ['夜间反复鸣叫'],
        'possible_causes': ['活动节律与环境刺激可能有关'], 'assessment': ['记录发生时间与伴随表现'],
        'interventions': [{'action': '白天安排适量互动游戏', 'reason': '提供活动机会'}],
        'observation_fields': ['叫声次数'], 'stop_conditions': ['突然变化或伴随疼痛时寻求兽医评估'],
        'source_urls': [], 'evidence_status': 'pending_verification'}]}


def db(existing=None):
    conn = MagicMock(); conn.__enter__.return_value = conn
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = {'id': 5, 'knowledge_base': 'behavior', 'status': 'success',
                                'parsed_response': json.dumps(document())}
    cur.fetchall.return_value = existing or []
    return conn, cur


def test_templates_resolve_for_behavior_without_changing_other_bases(monkeypatch, tmp_path):
    monkeypatch.setattr(prompts, 'SETTINGS_PATH', tmp_path / 'templates.json')
    bases = prompts.collection_bases()['bases']
    base = next(b for b in bases if b['code'] == 'behavior')
    assert len(base['templates']) == 2
    for template in base['templates']:
        assert '{{question}}' in template['template'] and '"behaviors"' in template['template']
    payload = {'knowledge_base': 'behavior', 'template_code': 'behavior_training',
               'question': document()['question'], 'providers': ['qwen']}
    with patch.object(run, 'provider_configuration', return_value=[run.Provider('qwen', 'test', '', 'test')]):
        assert multi.validate(payload)[3] == 'behavior'
    assert {'selection', 'health', 'nutrition', 'solution'}.issubset({b['code'] for b in bases})


def test_collector_validates_and_deduplicates_behavior_points():
    doc = document(); doc['behaviors'] *= 2
    parsed = run.extract_json('```json\n' + json.dumps(doc) + '\n```', knowledge_base='behavior')
    assert parsed == document() and run.count_points(parsed) == 1
    with pytest.raises(ValueError):
        run.extract_json(json.dumps({'dimensions': {}}), knowledge_base='behavior')


@pytest.mark.parametrize('field,value', [('species', 'bird'), ('category', 'unknown'),
    ('manifestations', []), ('assessment', []), ('interventions', []),
    ('interventions', ['打骂']), ('source_urls', ['javascript:alert(1)']),
    ('evidence_status', 'verified'), ('breed', None)])
def test_malformed_behavior_is_rejected(field, value):
    doc = document(); doc['behaviors'][0][field] = value
    with pytest.raises(ValueError):
        behavior.validate_document(doc)


def test_behavior_import_uses_dedicated_table_and_preserves_provenance():
    conn, cur = db()
    with patch.object(store, 'ensure_database'), patch.object(store, 'connect', return_value=conn):
        result = behavior.import_batch(5)
    assert result['inserted_count'] == 1
    inserts = [c.args for c in cur.execute.call_args_list if c.args[0].startswith('INSERT')]
    assert len(inserts) == 1 and 'behavior_knowledge_point_detail' in inserts[0][0]
    assert inserts[0][1][0] == 5 and json.loads(inserts[0][1][-1]) == document()['behaviors'][0]
    assert "DEFAULT 'pending'" in behavior.DDL
    conn.commit.assert_called_once()


def test_retry_preserves_review_and_rejects_changed_reviewed_content():
    key = hashlib.sha256(store.dumps(document()['behaviors'][0]).encode()).hexdigest()
    conn, cur = db([{'point_key': key, 'review_status': 'verified'}])
    with patch.object(store, 'ensure_database'), patch.object(store, 'connect', return_value=conn):
        assert behavior.import_batch(5)['status'] == 'already_imported'
    assert not any(c.args[0].startswith(('DELETE', 'INSERT')) for c in cur.execute.call_args_list)
    conn, cur = db([{'point_key': 'old', 'review_status': 'verified'}])
    with patch.object(store, 'ensure_database'), patch.object(store, 'connect', return_value=conn):
        with pytest.raises(ValueError, match='人工审核'):
            behavior.import_batch(5)
    conn.rollback.assert_called_once(); conn.commit.assert_not_called()


def test_import_dispatch_bypasses_selection_pipeline():
    conn, cur = db()
    with patch.object(store, 'ensure_database'), patch.object(store, 'connect', return_value=conn), \
         patch.object(behavior, 'import_batch', return_value={'status': 'completed'}) as importer, \
         patch.object(store, '_import_batch_source') as selection:
        assert store.import_batch(5)['status'] == 'completed'
    importer.assert_called_once_with(5); selection.assert_not_called()


def test_search_filters_and_pagination_are_parameterized():
    conn, cur = db(); cur.fetchone.return_value = {'n': 21}; cur.fetchall.return_value = []
    with patch.object(store, 'ensure_database'), patch.object(store, 'connect', return_value=conn):
        result = behavior.list_points(page=2, query='夜叫%_', species='cat', category='vocalization')
    sql, params = cur.execute.call_args.args
    assert "b.knowledge_base='behavior'" in sql and '夜叫' not in sql
    assert params == ['cat', 'vocalization', '%夜叫!%!_%', '%夜叫!%!_%', 20, 20]
    assert result['total'] == 21


def test_behavior_api_requires_auth_and_returns_validation_errors():
    app = Flask(__name__); app.secret_key = 'test'; app.register_blueprint(enterprise_api)
    client = app.test_client()
    with patch.dict('os.environ', {'MINIPROGRAM_ADMIN_TOKEN': 'test-only'}):
        assert client.get('/api/admin/multi-model/behavior-points').status_code == 401
    with client.session_transaction() as session:
        session['workbench_user'] = {'role': 'data_admin'}
    assert client.get('/api/admin/multi-model/behavior-points?species=bird').status_code == 400
    with patch.object(behavior, 'list_points', return_value={'items': [], 'total': 0}):
        assert client.get('/api/admin/multi-model/behavior-points').get_json()['ok']


def test_worker_passes_behavior_contract_to_all_selected_providers(monkeypatch):
    seen = []
    def call(provider, prompt, **kwargs):
        seen.append(kwargs)
        return {'provider': provider.name, 'status': 'success', 'parsed': document(), 'latency_ms': 1, 'error': None}
    monkeypatch.setattr(run, 'call_provider', call)
    monkeypatch.setattr(run, 'persist_result', lambda *a: None)
    monkeypatch.setattr(run, 'update_task', lambda *a: None)
    run.execute_task(1, 'prompt', [run.Provider('qwen', 'test', '', 'test')],
                     knowledge_base='behavior', question='question')
    assert seen[0]['knowledge_base'] == 'behavior'


def test_failed_behavior_response_can_be_reparsed_without_model_calls():
    conn, cur = db()
    cur.fetchone.return_value = {'knowledge_base': 'behavior', 'status': 'parse_failed',
        'question': document()['question'], 'raw_response': json.dumps(document()), 'search_metadata': None}
    with patch.object(store, 'connect', return_value=conn), patch.object(run, 'call_provider') as model:
        store.reparse_behavior_batch(5)
    model.assert_not_called()
    assert any("status='success'" in c.args[0] for c in cur.execute.call_args_list)


def test_task_detail_uses_behavior_candidates_without_selection_assembly():
    conn, cur = db()
    batch = {'id':5, 'question':document()['question'], 'prompt_template':'{{question}}',
             'web_search':False, 'status':'success', 'knowledge_base':'behavior', 'provider':'qwen',
             'model':'test', 'raw_response':'raw', 'parsed_response':json.dumps(document()),
             'search_metadata':None, 'error_message':None, 'import_error':None,
             'prompt_tokens':1, 'completion_tokens':2}
    cur.fetchall.side_effect = [[batch], []]
    with patch.object(multi, 'ensure_tables'), patch.object(store, 'connect', return_value=conn), \
         patch.object(store, 'task_status', return_value={}), \
         patch.object(behavior, 'list_points', return_value={'items':[], 'total':0}) as points, \
         patch('services.knowledge_source_assembly_service.task_objects') as assembly:
        result = multi.task_detail(5)
    assert result['behavior_structured']['total'] == 0 and result['assembled_points'] == []
    points.assert_called_once_with(task_id=5); assembly.assert_not_called()


def observation_document():
    return {'question': '猫咪吐毛球', 'species': 'felis_catus', 'breed': '', 'life_stage': '',
        'observations': [{'observation_code': 'vomiting_hairball', 'observation_name': '吐毛球',
        'domain_code': 'digestive', 'category_code': 'vomiting', 'observation_type': 'physiological_event',
        'aliases': ['吐毛团'], 'observable_features': ['吐出毛发团'], 'related_observations': ['呕吐'],
        'normal_scenarios': [{'scenario': '偶发情况', 'mechanism': '模型原文', 'conditions': ['其他状态正常'],
                              'observable_signs': ['吐出毛发团'], 'source_ids': ['unverified-source']}],
        'abnormal_scenarios': [{'boundary_id': 'B1', 'scenario': '持续不适', 'conditions': ['反复出现'],
            'warning_signs': ['精神变化'], 'risk_level': 'veterinary_review', 'source_ids': [],
            'health_relations': [{'health_issue_code': None, 'health_issue_name': '待核验问题',
                'relation_type': 'possible_manifestation', 'mechanism_summary': '可能机制，待审核',
                'supporting_signs': ['持续不适'], 'differentiating_signs': [], 'verification': ['兽医评估'],
                'source_knowledge_ids': []}]}],
        'unmapped_health_issues': [{'health_issue_name': '待核验问题', 'status': 'pending_review'}],
        'evidence_status': 'pending_review'}]}


def test_observation_contract_preserves_scenes_boundaries_and_candidates_losslessly():
    original = observation_document()
    original['observations'][0]['related_observations'].append({'observation_code':'dry_heaving', 'observation_name':'干呕', 'relation':'鉴别现象'})
    parsed = run.extract_json('搜索说明\n```json\n' + json.dumps(original) + '\n```', knowledge_base='behavior')
    point = parsed['behaviors'][0]
    assert point['observation'] == original['observations'][0]
    assert point['species'] == 'cat' and point['life_stage'] == '未限定'
    assert point['evidence_status'] == 'pending_verification'
    assert point['record_type'] == 'observation' and 'interventions' not in point
    assert behavior.validate_document(parsed) == parsed
    assert run.count_points(parsed) == 1


def test_invalid_observation_boundary_is_rejected():
    doc = observation_document()
    doc['observations'][0]['abnormal_scenarios'][0]['risk_level'] = 'diagnosed'
    with pytest.raises(ValueError, match='risk_level'):
        behavior.validate_document(doc)


def test_observation_retry_and_import_do_not_create_formal_health_relations():
    conn, cur = db()
    cur.fetchone.return_value['parsed_response'] = json.dumps(observation_document())
    with patch.object(store, 'ensure_database'), patch.object(store, 'connect', return_value=conn):
        result = behavior.import_batch(5)
    assert result['inserted_count'] == 1
    writes = [c.args for c in cur.execute.call_args_list if c.args[0].startswith('INSERT')]
    assert len(writes) == 1 and 'behavior_knowledge_point_detail' in writes[0][0]
    assert json.loads(writes[0][1][-1])['observation'] == observation_document()['observations'][0]


def test_observation_prompt_placeholders_are_explicit_without_invented_context():
    rendered = behavior.render_prompt('问题{{question}} 物种{{species}} 品种{{breed}} 阶段{{life_stage}} 字典{{existing_observations}} 疾病{{disease_candidates}} 来源{{reference_materials}}', '猫咪吐毛球')
    assert '{{' not in rendered and '猫咪吐毛球' in rendered
    assert '疾病[]' in rendered and '字典[]' in rendered and '未提供' in rendered
    assert behavior.render_prompt('{{question}}', '{{species}}') == '{{species}}'
