import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from services import product_recommendation_import_service as service
from vendor.pet_product_import import import_products as parser
from scripts import import_product_recommendations as cli

EXAMPLE = Path(__file__).resolve().parents[1] / 'vendor/pet_product_import/example_products.json'


def item():
    return json.loads(EXAMPLE.read_text())[0]


def group(entry=None, **scope):
    return {'id': 2, 'category': 'product_recommendation', 'species_code': 'cat',
            'breed_code': 'ragdoll', 'life_stage_code': 'kitten', 'need_scene_code': 'food_selection',
            'content': '汇总中可能出现其他名称，不能从这里抽取商品',
            'sources': json.dumps([entry or item()], ensure_ascii=False), **scope}


def test_nested_database_export_uses_raw_source_not_summary_and_preserves_scope():
    result = parser.normalize([group()])
    assert not result['errors'] and len(result['records']) == 1
    r = result['records'][0]
    assert r['brand'] == '渴望' and r['knowledge_source_id'] == 2
    assert r['original_source_id'] == 4807
    assert r['recommendations'][0]['breed_code'] == 'ragdoll'
    assert r['recommendations'][0]['life_stage_code'] == 'kitten'
    assert r['recommendations'][0]['review_status'] == 'pending'
    assert r['verification_status'] == 'pending'


def test_each_model_source_retains_its_own_needs_and_conditions():
    a = item(); b = deepcopy(a['sources'][0]); b['id'] += 1
    b['need_codes'] = ['hydration']; b['source_context']['structured_item']['applicable_conditions'] = ['成年猫']
    a['sources'].append(b)
    result = parser.normalize([a])
    x, y = result['records']
    assert x['product_key'] == y['product_key'] and x['source_key'] != y['source_key']
    assert {r['need_code'] for r in x['recommendations']} == {'coat_health'}
    assert {r['need_code'] for r in y['recommendations']} == {'hydration'}
    assert {r['life_stage_code'] for r in y['recommendations']} == {'adult'}


def test_same_source_across_scopes_keeps_context_separate_and_is_deterministic():
    rows = [group(), group(id=3, life_stage_code='adult')]
    result = parser.normalize(rows)
    a, b = result['records']
    assert a['product_key'] == b['product_key'] and a['source_key'] != b['source_key']
    assert parser.normalize(rows) == result
    assert len(parser.normalize(rows + rows)['records']) == 2


def test_cat_dog_claims_require_explicit_object_scope_and_have_distinct_keys():
    entry = item(); entry['sources'][0]['source_context']['structured_item']['applicable_conditions'] = ['全年龄犬猫']
    assert parser.normalize([entry])['errors']
    rows = [group(entry), group(entry, id=3, species_code='dog', breed_code='all_dog')]
    result = parser.normalize(rows)
    assert not result['errors']
    assert result['records'][0]['product_key'] != result['records'][1]['product_key']


def test_puppy_and_multiple_life_stages_are_not_discarded():
    entry = item(); src = entry['sources'][0]
    src['source_context']['question'] = '狗狗吃什么'
    src['source_context']['structured_item']['product_name'] = '六种鱼全价狗粮'
    src['source_context']['structured_item']['applicable_conditions'] = ['幼犬和成犬']
    r = parser.normalize([entry])['records'][0]
    assert r['species_code'] == 'dog'
    assert {x['life_stage_code'] for x in r['recommendations']} == {'puppy', 'adult'}


def test_source_context_is_not_misrepresented_as_breed_exclusive():
    r = parser.normalize([item()])['records'][0]
    assert r['recommendations'][0]['relation_type'] == 'source_context'
    entry = item(); entry['sources'][0]['source_context']['structured_item']['product_name'] = '布偶猫专用粮'
    r = parser.normalize([entry])['records'][0]
    assert r['recommendations'][0]['relation_type'] == 'explicit_product'


def test_scene_only_codes_are_filtered_and_unknown_product_identity_fails():
    entry = item(); entry['sources'][0]['need_codes'] = ['food_selection', 'product_recommendation']
    assert parser.normalize([entry])['records'][0]['recommendations'][0]['need_code'] == 'general_recommendation'
    entry['sources'][0]['id'] = None
    assert parser.normalize([entry])['errors']
    assert parser.normalize([{'category': 'product_recommendation', 'content': '只有总结'}])['errors']
    assert parser.normalize([{'category': 'food_selection'}]) == {'records': [], 'errors': []}


def test_different_flavours_stay_distinct_and_supported_is_not_verified():
    a = item(); b = deepcopy(a)
    b['sources'][0]['source_context']['structured_item']['product_name'] += '鸡肉款'
    records = parser.normalize([a, b])['records']
    assert len({r['product_key'] for r in records}) == 2
    assert all(x['verification_status'] == 'pending' for x in records)


def test_default_cli_preview_never_initializes_or_writes_target(capsys):
    with patch.object(service, 'import_records', side_effect=AssertionError('preview cannot write')):
        assert cli.main(['--input', str(EXAMPLE)]) == 0
    assert json.loads(capsys.readouterr().out)['mode'] == 'dry-run'


def test_cli_aborts_entire_apply_on_parse_errors(tmp_path, capsys):
    p = tmp_path / 'bad.json'; p.write_text(json.dumps([item(), {}]))
    with patch.object(service, 'import_records') as write:
        assert cli.main(['--input', str(p), '--apply']) == 1
    write.assert_not_called()
    assert json.loads(capsys.readouterr().out)['write_status'] == 'aborted_before_write'


def test_database_selection_is_parameterized_and_read_only():
    c = MagicMock(); cur = c.__enter__.return_value.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = []
    with patch.object(service.source_store, 'connect', return_value=c):
        service.read_sources(source_id=2, after_id=0, limit=10)
    sql, params = cur.execute.call_args.args
    assert sql.startswith('SELECT') and params == ['product_recommendation', 0, 2, 10]
    c.commit.assert_not_called()


def test_unchanged_source_does_not_rewrite_reviewed_records():
    r = service.group_records(parser.normalize([group()])['records'])[0]
    conn = MagicMock(); cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = {'id': 7, 'input_hash': parser.digest(r), 'review_status': 'verified', 'verification_status': 'pending'}
    assert service.write_record(conn, r) == (7, 'unchanged')
    assert cur.execute.call_count == 1


def test_changed_reviewed_source_is_not_overwritten():
    conn = MagicMock(); cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.side_effect = [{'id': 1, 'input_hash': 'old', 'review_status': 'verified', 'verification_status': 'pending'}]
    with pytest.raises(ValueError, match='人工审核'):
        service.write_record(conn, service.group_records(parser.normalize([group()])['records'])[0])
    assert all('DELETE' not in c.args[0] for c in cur.execute.call_args_list)


def test_failed_batch_rolls_back_every_record():
    conn = MagicMock(); conn.__enter__.return_value = conn
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = {'acquired': 1}; cur.fetchall.return_value = []
    with patch.object(service, 'ensure_tables'), patch.object(service, 'target_connect', return_value=conn), \
         patch.object(service, 'write_record', side_effect=ValueError('bad record')):
        with pytest.raises(ValueError): service.import_records(parser.normalize([group()])['records'])
    conn.rollback.assert_called_once(); conn.commit.assert_not_called()


def test_single_table_insert_contains_dimensions_and_all_provenance():
    record = service.group_records(parser.normalize([group()])['records'])[0]
    conn = MagicMock(); cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = None
    service.write_record(conn, record)
    sql, values = cur.execute.call_args.args
    assert sql.startswith('INSERT INTO product_recommendation ')
    fields = sql.split('(', 1)[1].split(')', 1)[0].split(',')
    inserted = dict(zip(fields, values))
    assert inserted['breed_code'] == 'ragdoll'
    assert inserted['life_stage_code'] == 'kitten'
    assert inserted['need_code'] == 'coat_health'
    assert inserted['brand'] == '渴望'
    source = json.loads(inserted['sources'])[0]
    assert source['original_source_id'] == 4807 and source['knowledge_source_id'] == 2
    assert source['source_model'] and source['recommendation']['recommendation_reason']
    assert json.loads(inserted['raw_json'])['records'][0]['sources']


def test_changed_pending_group_updates_same_row_with_provenance():
    record = service.group_records(parser.normalize([group()])['records'])[0]
    conn = MagicMock(); cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = {'id': 7, 'input_hash': 'old',
                               'review_status': 'pending', 'verification_status': 'pending'}
    assert service.write_record(conn, record) == (7, 'imported')
    sql, values = cur.execute.call_args.args
    assert sql.startswith('UPDATE product_recommendation SET ')
    assert 'sources=%s' in sql and values[-1] == 7


def test_six_dimensions_merge_sources_without_losing_reasons_or_model_metadata():
    a = item(); b = deepcopy(a)
    src = b['sources'][0]; src['id'] = 9999
    src['source_model'] = 'another-model'; src['source_provider'] = 'another-provider'
    src['source_context']['structured_item']['reason'] = '另一个模型的推荐理由'
    records = parser.normalize([group(a), group(b, id=3)])['records']
    result = service.group_records(records)
    assert len(result) == 1
    row = result[0]
    assert row['source_count'] == row['model_count'] == 2
    assert {s['original_source_id'] for s in row['sources']} == {4807, 9999}
    assert {s['knowledge_source_id'] for s in row['sources']} == {2, 3}
    assert '另一个模型的推荐理由' in row['recommendation_reason']
    assert row['recommendation_key'] == service.group_records(records[:1])[0]['recommendation_key']
    assert service.group_records(list(reversed(records))) == result
    assert service.group_records(records + records) == result


def test_each_dimension_stays_separate_and_all_stages_is_not_expanded():
    scopes = [{}, {'id': 3, 'life_stage_code': 'adult'}, {'id': 4, 'breed_code': 'maine_coon'},
              {'id': 5, 'life_stage_code': 'all_stages'}]
    records = parser.normalize([group(**scope) for scope in scopes])['records']
    rows = service.group_records(records)
    assert len(rows) == 4
    assert {r['life_stage_code'] for r in rows} == {'kitten', 'adult', 'all_stages'}
    a = item(); a['sources'][0]['need_codes'] = ['coat_health', 'hydration']
    assert {r['need_code'] for r in service.group_records(parser.normalize([group(a)])['records'])} == {'coat_health', 'hydration'}
    b = item(); b['sources'][0]['source_context']['structured_item']['product_name'] += '鸡肉款'
    assert len(service.group_records(parser.normalize([group(), group(b)])['records'])) == 2
    b['sources'][0]['source_context']['structured_item']['brand'] = '另一品牌'
    assert len(service.group_records(parser.normalize([group(), group(b)])['records'])) == 2


def test_grouped_storage_round_trips_original_records_and_rejects_conflicting_sources():
    records = parser.normalize([group(), group(id=3, life_stage_code='adult')])['records']
    rows = service.group_records(records)
    assert service.stored_records(rows) == {r['source_key']: r for r in records}
    conflict = deepcopy(records[0]); conflict['brand'] = 'changed'
    with pytest.raises(ValueError): service.group_records([records[0], conflict])


def test_migration_refuses_manually_reviewed_rows():
    from scripts.merge_product_recommendations import legacy_records
    row = {'raw_json': parser.normalize([group()])['records'][0],
           'review_status': 'verified', 'verification_status': 'pending'}
    with pytest.raises(ValueError, match='人工审核'): legacy_records([row])
    row['review_status'] = 'pending'
    assert legacy_records([row]) == [row['raw_json']]
