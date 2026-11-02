#!/usr/bin/env python3
"""Operate on a detached copy only. This program cannot fence Hermes writers."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys

if os.environ.get('HERMES_AGENT_DIR'):
    sys.path.insert(0, os.environ['HERMES_AGENT_DIR'])
source = Path(__file__).resolve().parents[1] / 'zvec-memory' / '__init__.py'
spec = importlib.util.spec_from_file_location('offline_zvec_provider', source,
                                               submodule_search_locations=[str(source.parent)])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
from offline_zvec_provider.offline_reconcile import create_snapshot, plan, apply


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    copy = sub.add_parser('snapshot')
    copy.add_argument('--profile', required=True)
    copy.add_argument('--vault', required=True)
    copy.add_argument('--destination', required=True)
    copy.add_argument('--writers-stopped', action='store_true', required=True,
                      help='Operator attestation only; this program cannot verify it')
    view = sub.add_parser('plan')
    view.add_argument('--snapshot', required=True)
    execute = sub.add_parser('apply')
    execute.add_argument('--snapshot', required=True)
    execute.add_argument('--confirm-plan-sha256', required=True)
    execute.add_argument('--writers-stopped', action='store_true', required=True,
                         help='Operator attestation only; this program cannot verify it')
    args = parser.parse_args()
    if args.command == 'snapshot':
        result = create_snapshot(args.profile, args.vault, args.destination)
    elif args.command == 'plan':
        result = plan(args.snapshot)
    else:
        result = apply(args.snapshot, args.confirm_plan_sha256)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
