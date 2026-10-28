import json
import sys
from unittest.mock import patch

from scripts import run_formula_phase as runner
from services import formula_incremental_service as service
import pytest


def test_phase_failure_keeps_prior_step_and_stops_later_steps(tmp_path):
    scripts = [('a', 'a.py'), ('b', 'b.py'), ('c', 'c.py')]
    (tmp_path / 'a.py').write_text("print('first complete')")
    (tmp_path / 'b.py').write_text("print('failure detail'); raise ValueError('bad score')")
    (tmp_path / 'c.py').write_text("raise AssertionError('must not run')")
    report = tmp_path / 'report.json'
    argv, paths = sys.argv[:], sys.path[:]
    with patch.object(runner, 'FEATURE', tmp_path), patch.dict(runner.PHASES, {'scores': scripts}):
        assert runner.run_phase('scores', 'batch', report) == 1
    data = json.loads(report.read_text())
    assert not data['ok'] and list(data['steps']) == ['a', 'b']
    assert data['steps']['a']['ok'] and not data['steps']['b']['ok']
    assert any('bad score' in line for line in data['steps']['b']['log_tail'])
    assert sys.argv == argv and sys.path == paths


def test_step_success_exit_and_bounded_individual_log(tmp_path):
    path = tmp_path / 'step.py'
    path.write_text("for i in range(100): print(i)\nraise SystemExit(0)")
    result = runner.run_step(path, [])
    assert result['ok'] and len(result['log_tail']) == 40
    assert result['log_tail'][-1] == '99'


def test_phase_report_rejects_partial_success_and_removes_temporary_files():
    paths = []
    def fake(command, **kwargs):
        from pathlib import Path
        path = Path(command[-1]);paths.append(path)
        path.write_text(json.dumps({'phase': 'scores', 'ok': True, 'steps': {'protein_score': {'ok': True}}}))
    with patch.object(service, '_run', side_effect=fake):
        with pytest.raises(RuntimeError, match='完整'):
            service._run_phase('scores', env={})
    assert all(not path.parent.exists() for path in paths)
