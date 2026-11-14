"""Normalize existing source points with Qwen and the imported standard dictionary."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from services.knowledge_standardization_service import normalize

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--batch-id',type=int)
    parser.add_argument('--force',action='store_true',help='重新调用模型处理已完成的数据')
    args=parser.parse_args()
    result=normalize(batch_id=args.batch_id,force=args.force)
    print(json.dumps(result,ensure_ascii=False),flush=True)
    sys.exit(1 if result['failed'] or result['changed'] or result['busy'] else 0)
