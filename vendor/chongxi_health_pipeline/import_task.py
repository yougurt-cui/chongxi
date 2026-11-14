#!/usr/bin/env python3
"""Run inside the chongxi server environment; never hardcode database credentials."""
import argparse
from pathlib import Path
import runpy
import sys
parser=argparse.ArgumentParser()
parser.add_argument('--project-root',type=Path,required=True)
parser.add_argument('--task-id',type=int,required=True)
parser.add_argument('--dry-run',action='store_true')
args=parser.parse_args()
root=args.project_root.resolve()
sys.path.insert(0,str(root))
sys.argv=[str(root/'scripts/import_health_task.py'),'--task-id',str(args.task_id)]+(['--dry-run'] if args.dry_run else [])
runpy.run_path(str(root/'scripts/import_health_task.py'),run_name='__main__')
