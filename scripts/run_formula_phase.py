"""Run one fixed calculation phase in an isolated, short-lived interpreter.

Legacy script globals stay separate; heavy library imports are shared only
within this phase. The web process never changes its environment or argv.
"""
from __future__ import annotations

import argparse
from collections import deque
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import runpy
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
FEATURE = ROOT / 'vendor/feature_score_pipeline'
PHASES = {
    'scores': (('protein_score', 'scripts/protein_score1.py'),
               ('fiber_score', 'scripts/fiber_remark_score.py'),
               ('fat_score', 'scripts/fat_score1.py'),
               ('wide', 'scripts/build_catfood_score_wide_table.py'),
               ('sku_feature', 'scripts/build_sku_feature_input.py')),
    'risks': (('black_chin', 'black_risk_done.py'), ('soft_stool', 'soft_risk_done.py')),
}


class StepLog(io.TextIOBase):
    def __init__(self, sink):
        self.sink, self.lines, self.pending = sink, deque(maxlen=40), ''

    def write(self, text):
        self.sink.write(text)
        parts = (self.pending + text).split('\n')
        self.lines.extend(line[-2000:] for line in parts[:-1])
        self.pending = parts[-1][-2000:]
        return len(text)

    def flush(self):
        self.sink.flush()

    def tail(self):
        return [*self.lines, *([self.pending] if self.pending else [])][-40:]


def run_step(path, arguments):
    previous_argv, previous_path = sys.argv[:], sys.path[:]
    log = StepLog(sys.stdout)
    started, returncode = time.monotonic(), 0
    try:
        sys.argv = [str(path), *arguments]
        sys.path.insert(0, str(path.parent))
        with redirect_stdout(log), redirect_stderr(log):
            try:
                runpy.run_path(str(path), run_name='__main__')
            except SystemExit as error:
                if error.code not in (None, 0):
                    raise
    except (Exception, SystemExit):
        returncode = 1
        traceback.print_exc(file=log)
    finally:
        sys.argv, sys.path = previous_argv, previous_path
    return {'ok': returncode == 0, 'returncode': returncode,
            'seconds': round(time.monotonic() - started, 6), 'log_tail': log.tail()}


def run_phase(phase, batch_id, report_path):
    report = {'phase': phase, 'ok': False, 'steps': {}}
    for name, relative in PHASES[phase]:
        arguments = []
        if phase == 'risks':
            arguments = ['--batch_id', batch_id, '--feature_version',
                         'v1' if name == 'black_chin' else 'soft_v1', '--no_reference_monitor']
        result = run_step(FEATURE / relative, arguments)
        report['steps'][name] = result
        report['ok'] = result['ok'] and len(report['steps']) == len(PHASES[phase])
        temporary = report_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(report, ensure_ascii=False), encoding='utf-8')
        temporary.replace(report_path)
        if not result['ok']:
            return 1
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=tuple(PHASES))
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if int(os.environ.get('FORMULA_ID', '0')) <= 0 or not os.environ.get('SKU_FEATURE_BATCH_ID'):
        parser.error('positive FORMULA_ID and SKU_FEATURE_BATCH_ID are required')
    return run_phase(args.phase, os.environ['SKU_FEATURE_BATCH_ID'], args.report)


if __name__ == '__main__':
    raise SystemExit(main())
