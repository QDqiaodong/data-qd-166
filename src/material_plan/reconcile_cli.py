"""material-reconcile 命令行入口：init / reconcile / approve / show。

成功退出 0，stdout 仅输出一份 JSON 和换行；
失败退出 2，stderr 输出错误，stdout 为空。
"""
import argparse
import json
import sys

from .common import read_json
from .reconcile import init_db, reconcile, approve, show


def main(argv=None):
    parser = argparse.ArgumentParser(prog='material-reconcile',
                                     description='采购到货核销')
    sub = parser.add_subparsers(dest='command', required=True)

    init_parser = sub.add_parser('init', help='初始化采购核销数据库')
    init_parser.add_argument('--db', required=True)
    init_parser.add_argument('--source', required=True)

    reconcile_parser = sub.add_parser('reconcile',
                                      help='提交完整到货证据并核对')
    reconcile_parser.add_argument('--db', required=True)
    reconcile_parser.add_argument('--source', required=True)

    approve_parser = sub.add_parser('approve', help='按当前证据放行合格品')
    approve_parser.add_argument('--db', required=True)
    approve_parser.add_argument('--source', required=True)

    show_parser = sub.add_parser('show', help='输出当前核对报告与凭证')
    show_parser.add_argument('--db', required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == 'init':
            result = init_db(args.db, read_json(args.source))
        elif args.command == 'reconcile':
            result = reconcile(args.db, read_json(args.source))
        elif args.command == 'approve':
            result = approve(args.db, read_json(args.source))
        else:  # show
            result = show(args.db)
        print(json.dumps(result, sort_keys=True, ensure_ascii=False,
                         allow_nan=False))
        return 0
    except (OSError, ValueError, TypeError, KeyError, OverflowError,
            RecursionError, json.JSONDecodeError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
