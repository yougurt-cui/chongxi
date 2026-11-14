#!/usr/bin/env python3
"""Import screenshot-community recommendation relations from the supplied XLSX template."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.screenshot_product_recommendation_import_service import normalize_workbook
from services.product_recommendation_import_service import group_records, import_records


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--sheet', default='产品推荐关系')
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--apply', action='store_true')
    parser.add_argument('--output', type=Path)
    args=parser.parse_args(argv)
    try:
        result=normalize_workbook(args.input,args.sheet)
        groups=group_records(result['records'])
        summary={'mode':'apply' if args.apply else 'dry-run','source_rows':result['source_rows'],
                 'records':len(result['records']),'groups':len(groups),
                 'brand_level':sum(r['entity_level']=='brand' for r in result['records']),
                 'product_level':sum(r['entity_level']=='product' for r in result['records']),
                 'errors':result['errors']}
        if args.output: args.output.write_text(json.dumps({**result,'grouped_records':groups},ensure_ascii=False,indent=2))
        if args.apply and result['errors']:summary['write_status']='aborted_before_write'
        elif args.apply and result['records']:summary.update(import_records(result['records']))
        print(json.dumps(summary,ensure_ascii=False))
        return 1 if result['errors'] else 0
    except Exception as exc:
        message=str(exc) if isinstance(exc,(ValueError,FileNotFoundError)) else '导入失败，请检查文件、连接或表结构'
        print(json.dumps({'status':'failed','error_type':type(exc).__name__,'message':message},ensure_ascii=False),file=sys.stderr)
        return 1


if __name__=='__main__':raise SystemExit(main())
