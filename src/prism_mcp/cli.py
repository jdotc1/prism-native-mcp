"""Diagnostic CLI calling the same backend as MCP."""
import argparse
import json
import sys
from .backend import Backend, project
from .errors import PrismError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('capabilities')
    inspect = sub.add_parser('inspect')
    inspect.add_argument('path')
    inspect.add_argument('--values', action='store_true')
    status = sub.add_parser('status')
    status.add_argument('run_id')
    args = parser.parse_args()
    try:
        if args.command == 'capabilities': value = Backend().capabilities()
        elif args.command == 'inspect': value = project(args.path, include_values=args.values)
        else: value = Backend().runtime.status(args.run_id)
        print(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False))
    except PrismError as exc:
        print(json.dumps({'ok': False, 'error': exc.as_dict()}), file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
