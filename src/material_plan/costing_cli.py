"""material-cost 命令行入口：init / close / show。

成功退出 0，stdout 仅输出一份 JSON 和换行；
失败退出 2，stderr 输出错误，stdout 为空。
"""
import argparse
import json
import sys

from .common import read_json
from .costing import init_db, close_period, show_period


def main(argv=None):
    parser = argparse.ArgumentParser(prog='material-cost',
                                     description='离线材料成本月末关账')
    sub = parser.add_subparsers(dest='command', required=True)

    init_parser = sub.add_parser('init', help='初始化成本账簿数据库')
    init_parser.add_argument('--db', required=True)

    close_parser = sub.add_parser('close', help='提交并输出一个新月的账单')
    close_parser.add_argument('--db', required=True)
    close_parser.add_argument('--source', required=True)

    show_parser = sub.add_parser('show', help='查询已冻结账单')
    show_parser.add_argument('--db', required=True)
    show_parser.add_argument('--period', required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == 'init':
            result = init_db(args.db)
        elif args.command == 'close':
            result = close_period(args.db, read_json(args.source))
        else:  # show
            result = show_period(args.db, args.period)
        print(json.dumps(result, sort_keys=True, ensure_ascii=False,
                         allow_nan=False))
        return 0
    except (OSError, ValueError, TypeError, KeyError, OverflowError,
            RecursionError, json.JSONDecodeError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
