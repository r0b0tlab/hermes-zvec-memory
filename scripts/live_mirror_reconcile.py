#!/usr/bin/env python3
"""Mirror cutover ONLY inside an independently controlled offline outage."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys

if os.environ.get('HERMES_AGENT_DIR'):
    sys.path.insert(0, os.environ['HERMES_AGENT_DIR'])
source = Path(__file__).resolve().parents[1] / 'zvec-memory' / '__init__.py'
spec = importlib.util.spec_from_file_location('cutover_zvec_provider', source,
                                               submodule_search_locations=[str(source.parent)])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
from cutover_zvec_provider.live_reconcile import prepare, commit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('prepare')
    p.add_argument('--profile', required=True)
    p.add_argument('--vault', required=True)
    p.add_argument('--snapshot', required=True)
    p.add_argument('--offline-controller-active', action='store_true', required=True,
                   help='attestation only, not a writer fence')
    c = sub.add_parser('commit')
    c.add_argument('--snapshot', required=True)
    c.add_argument('--confirm-plan-sha256', required=True)
    c.add_argument('--offline-controller-active', action='store_true', required=True,
                   help='attestation only, not a writer fence')
    args = parser.parse_args()
    result = (prepare(args.profile, args.vault, args.snapshot) if args.command == 'prepare'
              else commit(args.snapshot, args.confirm_plan_sha256))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
