#!/usr/bin/env python3
"""Retired: plugin deployment belongs to the Hermes plugin manager."""
import sys


def main(argv=None):
    print(
        "Retired: scripts/upgrade.py performs no tests, installation, or rollback. "
        "Use a separately approved, exact-revision Hermes plugin installation. "
        "Old zvec-upgrade backups are historical evidence, not validated "
        "whole-generation rollback images.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
