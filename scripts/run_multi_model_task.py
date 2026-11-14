#!/usr/bin/env python3
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from services.multi_model_knowledge_service import run_worker,fail_running
if __name__=='__main__':
    task_id=int(sys.argv[1])
    try:run_worker(task_id)
    except Exception:
        fail_running(task_id)
        raise
