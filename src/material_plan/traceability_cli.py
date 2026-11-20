"""material-trace 命令行入口：init / apply / snapshot。

成功退出 0，stdout 仅输出一份 JSON 快照和换行；
失败退出 2，stderr 输出错误，stdout 为空。
"""
import argparse
import json
import sys

from .common import read_json
from .traceability import init_db, apply_batch, snapshot


def main(argv=None):
    parser = argparse.ArgumentParser(prog='material-trace',
                                     description='实物批次谱系与召回出库闸')
    sub = parser.add_subparsers(dest='command', required=True)

    init_parser = sub.add_parser('init', help='初始化实物账数据库')
    init_parser.add_argument('--db', required=True)
    init_parser.add_argument('--source', required=True)

    apply_parser = sub.add_parser('apply', help='提交收货/完工/发货/召回批次')
    apply_parser.add_argument('--db', required=True)
    apply_parser.add_argument('--source', required=True)

    snapshot_parser = sub.add_parser('snapshot', help='输出当前快照')
    snapshot_parser.add_argument('--db', required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == 'init':
            result = init_db(args.db, read_json(args.source))
        elif args.command == 'apply':
            result = apply_batch(args.db, read_json(args.source))
        else:  # snapshot
            result = snapshot(args.db)
        print(json.dumps(result, sort_keys=True, ensure_ascii=False,
                         allow_nan=False))
        return 0
    except (OSError, ValueError, TypeError, KeyError, OverflowError,
            RecursionError, json.JSONDecodeError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
