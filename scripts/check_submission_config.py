"""Read effective configuration without printing credentials or making requests."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from app_config import get_wechat_miniprogram_config
from services.submission_notifications import config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--require-worker', action='store_true')
    parser.add_argument('--require-notifications', action='store_true')
    args = parser.parse_args()
    enabled = os.getenv('CHONGXI_PIPELINE_WORKER', '0') == '1'
    try:
        concurrency = int(os.getenv('CHONGXI_PIPELINE_CONCURRENCY', '1'))
        sweep = int(os.getenv('CHONGXI_PIPELINE_SWEEP_SECONDS', '60'))
        worker_valid = 1 <= concurrency <= 4 and sweep >= 5
    except ValueError:
        concurrency = sweep = 'invalid'
        worker_valid = False
    admin = bool(os.getenv('MINIPROGRAM_ADMIN_TOKEN', '').strip())
    wechat = get_wechat_miniprogram_config()
    notice = config()
    state = os.getenv('WECHAT_MINIPROGRAM_STATE', 'formal')
    notice_valid = notice['enabled'] and bool(wechat['appid'] and wechat['appsecret']) and state in {'formal', 'trial', 'developer'}
    print('Configuration file:', BASE / '.env')
    print('Environment variables take precedence over that file.')
    print('Worker enabled:', enabled)
    print('Concurrent tasks:', concurrency, '| Fallback sweep seconds:', sweep)
    print('Admin token configured:', admin)
    print('Mini-program AppID:', wechat['appid'])
    print('AppSecret configured:', bool(wechat['appsecret']))
    print('Template ID and field mapping valid:', notice['enabled'])
    print('Notice destination:', state)
    print('Notification configuration ready:', bool(notice_valid))
    print('This does not start a worker, connect to MySQL, send a notice, or verify WeChat permissions.')
    return int((args.require_worker and not (enabled and worker_valid and admin)) or
               (args.require_notifications and not (enabled and worker_valid and admin and notice_valid)))


if __name__ == '__main__':
    raise SystemExit(main())
