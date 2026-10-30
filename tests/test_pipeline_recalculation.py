"""Scheduling contracts; real MySQL locking/transactions have a separate suite."""
from unittest.mock import MagicMock, patch

import pytest

from services import pipeline_recalculation_service as queue
from services import orchestrator_service as jobs
from services import formula_calculation_service as calc
from services import orchestrator_worker as worker


def connection():
    conn = MagicMock()
    conn.__enter__.return_value = conn
    return conn, conn.cursor.return_value.__enter__.return_value


@pytest.mark.parametrize('status,node,finished', [
    ('processing', 'success', False), ('created', 'pending', False),
    ('waiting_input', 'pending', False), ('done', 'running', False),
    ('done', 'waiting_result', False), ('need_review', 'running', False),
    ('done', 'success', True), ('need_review', 'need_review', True),
    ('failed', 'failed', True), ('cancelled', 'success', False),
])
def test_round_must_stop_and_have_no_inflight_nodes(status, node, finished):
    assert queue.round_finished({'task_status': status, 'nodes': [{'node_status': node}]}) is finished


def test_manual_formalization_validates_task_formula_before_request():
    task = {'outputs': {'formula_input_build': {'formula_id': 7}}}
    with patch.object(jobs, 'get_task', return_value=task), patch.object(queue, 'request') as request:
        with pytest.raises(ValueError, match='不匹配'):
            jobs.start_formula_formalization('t', 8)
        request.assert_not_called()
        result = jobs.start_formula_formalization('t', 7)
    assert result['status'] == 'waiting'
    request.assert_called_once_with('t', 'formula_input_build', '人工申请正式重算')


def test_cancelled_request_never_reactivates_task():
    conn, cur = connection()
    cur.fetchone.return_value = {'task_type': 'catfood_image_analysis', 'task_status': 'cancelled'}
    with patch.object(jobs, 'init_db'), patch.object(jobs, '_connect', return_value=conn), patch.object(jobs, 'wake_dispatcher') as wake:
        with pytest.raises(ValueError, match='撤销'):
            queue.request('t')
    wake.assert_not_called()


@pytest.mark.parametrize('entry,args', [('requeue_sources', [1]), ('requeue_formulas', [7])])
def test_review_and_dependency_entrypoints_only_record_intent(entry, args, monkeypatch):
    monkeypatch.setenv('CHONGXI_VERSIONED_CALCULATION', '1')
    conn, cur = connection()
    cur.fetchall.return_value = [{'id': 't'}]
    with patch.object(calc, 'connect', return_value=conn), patch.object(queue, 'request') as request, patch.object(queue, 'request_many', return_value=['t']) as many, patch.object(jobs, 'reset_node_for_reextract') as reset:
        assert getattr(calc, entry)(args) == ['t']
    assert request.call_count + many.call_count == 1
    reset.assert_not_called()


@pytest.mark.parametrize('change,science,blocked', [
    ({'confirmation_status': 'provisional'}, None, True),
    ({'result_stage': 'provisional'}, None, True),
    ({'unmatched_ingredient_count': 1}, None, True),
    ({'overall_status': 'provisional_ready'}, None, True),
    ({}, {'total': 1, 'missing': 1}, True),
    ({}, {'total': 0, 'missing': None}, True),
    ({}, {'total': 2, 'missing': 0}, False),
])
def test_formal_gate_requires_identity_ingredients_and_active_science(change, science, blocked):
    conn, cur = connection()
    cur.fetchone.side_effect = [{
        'confirmation_status': 'confirmed', 'result_stage': 'official',
        'overall_status': 'ready_for_rebuild', 'unmatched_ingredient_count': 0, **change,
    }, science]
    with patch.object(jobs, '_connect', return_value=conn):
        assert bool(queue.formal_gate(7)) is blocked


def test_running_round_does_not_refresh_working_inputs():
    conn, cur = connection()
    cur.fetchone.return_value = {'acquired': 1}
    task = {'task_status': 'processing', 'nodes': []}
    with patch.object(jobs, '_connect', return_value=conn), patch.object(jobs, 'get_task', return_value=task), patch.object(queue, 'formal_gate') as gate, patch.object(jobs, '_reset_task_nodes') as reset:
        assert queue._dispatch({'task_id': 't', 'generation': 1}) == '等待当前流程结束'
    gate.assert_not_called()
    reset.assert_not_called()


def test_lost_worker_lock_does_not_touch_task():
    conn, cur = connection()
    cur.fetchone.return_value = {'acquired': 0}
    with patch.object(jobs, '_connect', return_value=conn), patch.object(jobs, 'get_task') as read:
        assert queue._dispatch({'task_id': 't', 'generation': 1}) == '当前流程仍在执行'
    read.assert_not_called()


def test_refreshes_review_metadata_without_running_score_materialization():
    with patch('services.catfood_standardization_service.build_formula_feature_input') as inputs, patch(
        'scripts.backfill_formula_ingredient_features.backfill_formula'
    ) as profile, patch.object(jobs, '_connect') as db:
        db.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value.fetchone.return_value = {}
        assert queue.formal_gate(7, refresh=True)
    assert inputs.call_count == 2
    profile.assert_called_once_with(7, apply=True, materialize_science_features=False)


def test_superseded_review_is_parked_and_queued_instead_of_immediate_reset():
    conn, cur = connection()
    cur.fetchone.side_effect = [{'task_status': 'processing'}, {'node_status': 'ready'}]
    with patch.object(jobs, '_connect', return_value=conn), patch.object(jobs, '_set_task_status') as task, patch.object(jobs, '_set_node_status') as node, patch.object(queue, 'request') as request, patch.object(jobs, 'reset_node_for_reextract') as reset:
        worker._wait_for_formal_review('t', 'formula_profile', '审核未完成')
    task.assert_called_once_with(conn, 't', jobs.TASK_NEED_REVIEW, '审核未完成')
    node.assert_called_once_with(conn, 't', 'formula_profile', jobs.NODE_NEED_REVIEW, '审核未完成')
    request.assert_called_once_with('t', reason='审核未完成')
    reset.assert_not_called()


@pytest.mark.parametrize('task,node', [
    ({'task_status': 'cancelled'}, {}),
    ({'task_status': 'processing'}, {'node_status': 'running', 'claim_token': 'new-worker'}),
])
def test_parking_old_worker_cannot_resurrect_cancelled_or_replaced_task(task, node):
    conn, cur = connection()
    cur.fetchone.side_effect = [task, node]
    with patch.object(jobs, '_connect', return_value=conn), patch.object(jobs, '_set_task_status') as update, patch.object(queue, 'request') as request:
        assert not worker._wait_for_formal_review('t', 'formula_profile', 'stale', claim_token='old-worker')
    update.assert_not_called()
    request.assert_not_called()


def test_batch_review_schedules_once_after_all_successful_edits():
    from services import catfood_standardization_service as standard
    conn, cur = connection()
    cur.fetchall.return_value = [
        {'item_id': 1, 'raw_name': '甲', 'formula_id': 7},
        {'item_id': 2, 'raw_name': '甲', 'formula_id': 8},
        {'item_id': 3, 'raw_name': '乙', 'formula_id': 7},
    ]
    events = []
    def resolve(item_id, payload):
        events.append(item_id)
        return {'formula_id': 7}
    def resume(formulas):
        assert events == [1, 3]
        assert formulas == [7, 8]
        return ['t']
    with patch.object(standard, '_connect', return_value=conn), patch.object(standard, 'resolve_ingredient_review', side_effect=resolve), patch.object(jobs, 'resume_ingredient_tasks_for_formula_ids', side_effect=resume) as schedule:
        result = standard.batch_resolve_ingredient_reviews({'items': [
            {'raw_name': '甲', 'standard_ingredient_id': 'STD1'},
            {'raw_name': '乙', 'standard_ingredient_id': 'STD2'},
        ]})
    schedule.assert_called_once()
    assert result['resumed_task_ids'] == ['t'] and result['pending'] == []


def test_interrupted_unknown_side_effects_still_require_manual_verification():
    conn, cur = connection()
    cur.fetchone.return_value = {'acquired': 1}
    task = {'task_status': 'need_review', 'nodes': [], 'error_message': '执行中断，等待核验'}
    with patch.object(jobs, '_connect', return_value=conn), patch.object(jobs, 'get_task', return_value=task), patch.object(queue, 'formal_gate') as gate:
        assert queue._dispatch({'task_id': 't', 'generation': 1}) == '执行中断，需先核验已有结果后恢复任务'
    gate.assert_not_called()


def test_publication_invalidation_and_queue_record_commit_together():
    conn, cur = connection()
    cur.fetchone.return_value = {'task_type': 'catfood_image_analysis', 'task_status': 'done'}
    def fail_invalidation(sql, params):
        if sql.startswith('UPDATE formula_calculation_current'):
            raise RuntimeError('invalidation write failed')
    cur.execute.side_effect = fail_invalidation
    with patch.object(jobs, 'init_db'), patch.object(jobs, '_connect', return_value=conn), patch.object(jobs, 'wake_dispatcher') as wake:
        with pytest.raises(RuntimeError, match='invalidation write failed'):
            queue.request_many(['t'], invalidate_formula_ids=[7])
    assert any('INSERT INTO pipeline_recalculation_request' in c.args[0] for c in cur.execute.call_args_list)
    conn.commit.assert_not_called()
    wake.assert_not_called()
