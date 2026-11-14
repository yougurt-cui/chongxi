#!/usr/bin/env python3
"""Normalize product recommendations from knowledge_point_source or JSON exports."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services import product_recommendation_import_service as service


def positive(value):
    n = int(value)
    if n <= 0: raise argparse.ArgumentTypeError('必须为正整数')
    return n


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--input', type=Path, help='扁平JSON或 knowledge_point_source 查询导出数组')
    inputs.add_argument('--from-db', action='store_true', help="读取 category='product_recommendation'")
    parser.add_argument('--source-id', type=positive, help='knowledge_point_source.id，仅用于--from-db')
    parser.add_argument('--after-id', type=int, default=0)
    parser.add_argument('--limit', type=positive)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--dry-run', action='store_true', help='默认：只读解析，不建库、不写入')
    modes.add_argument('--apply', action='store_true', help='初始化独立候选产品库并事务导入')
    parser.add_argument('--output', type=Path, help='保存完整标准化结果和错误')
    args = parser.parse_args(argv)
    if args.after_id < 0: parser.error('--after-id不能为负数')
    if not args.from_db and (args.source_id or args.after_id or args.limit):
        parser.error('--source-id/--after-id/--limit仅用于--from-db')
    try:
        data = (service.read_sources(source_id=args.source_id, after_id=args.after_id, limit=args.limit)
                if args.from_db else json.loads(args.input.read_text(encoding='utf-8-sig')))
        if args.source_id and not data:
            raise ValueError('未找到指定的产品推荐知识对象')
        result = service.normalize(data)
        if args.output:
            args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        summary = {'mode': 'apply' if args.apply else 'dry-run', 'source_rows': len(data),
                   'records': len(result['records']), 'products': len({r['product_key'] for r in result['records']}),
                   'recommendations': sum(len(r['recommendations']) for r in result['records']),
                   'errors': result['errors']}
        if args.apply and not result['errors'] and result['records']:
            summary.update(service.import_records(result['records']))
        elif args.apply and result['errors']:
            summary['write_status'] = 'aborted_before_write'
        print(json.dumps(summary,ensure_ascii=False))
        return 1 if result['errors'] else 0
    except Exception as exc:
        # Never print connection strings, passwords, SQL parameters or provider payloads.
        message = str(exc) if isinstance(exc, (ValueError, FileNotFoundError)) else '执行失败，请检查连接、权限和表结构'
        print(json.dumps({'error_type':type(exc).__name__, 'status':'failed', 'message':message},ensure_ascii=False),file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
