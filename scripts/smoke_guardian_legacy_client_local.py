"""Run isolated local smoke and verify responses with the pre-P0 mini-program code."""
import argparse
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts')]
from flask.testing import FlaskClient
import smoke_guardian_p0_local
import smoke_soft_stool_local

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--client-repo', type=Path, default=ROOT.parent / 'xiaochengxu')
parser.add_argument('--client-ref', default='2c4813f7296e3a622858b109b185a52f1b3afdce')
args = parser.parse_args()
client_repo = str(args.client_repo.resolve())
ref = subprocess.check_output(['git', '-C', client_repo, 'rev-parse', '--verify', '--end-of-options', args.client_ref + '^{commit}'], text=True).strip()
responses = []
original = FlaskClient.open
def collect(client, *args, **kwargs):
    response = original(client, *args, **kwargs)
    body = response.get_json(silent=True)
    if body and response.status_code < 400:
        responses.append({'path': str(args[0]), 'method': kwargs.get('method', 'GET'),
                          'status': response.status_code, 'body': body})
    return response

with patch.object(FlaskClient, 'open', collect):
    smoke_guardian_p0_local.run()
    smoke_soft_stool_local.run()

from services import miniprogram_food_submission_service as uploads
submissions = []
for status in ('pending', 'processing', 'unrecognized', 'needs_review', 'failed', 'success'):
    submissions.append(uploads._serialize({'id': status, 'status': 'active',
        'recognition_status': status, 'ingredient_composition': '鸭肉，米' if status == 'success' else '',
        'review_status': 'pending', 'publish_status': 'unpublished'}))
submissions.append(uploads._serialize({'id': 'cancelled', 'status': 'cancelled',
    'recognition_status': 'success', 'ingredient_composition': '鸭肉，米'}))
submissions.append(next(r['body']['item'] for r in responses if '/food-submissions/' in r['path']))
subprocess.run(['node', str(ROOT / 'scripts/check_guardian_legacy_client.cjs'),
    client_repo, ref],
    input=json.dumps({'responses': responses, 'submissions': submissions}, ensure_ascii=False),
    text=True, check=True)
