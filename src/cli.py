#!/usr/bin/env python3
"""calibre-utils — maintenance tools for a Calibre library.

Usage:
    calibre-utils <command> [options]
    calibre-utils <command> --help

Commands:
    dedup       Merge duplicate book records (quarantine + manifest)
    fix-sort    Backfill author_sort and title sort fields

This runs inside Calibre's bundled Python (the commands import calibre.*).
Normally it is launched via the `calibre-utils` wrapper at the repo root; it can
also be run directly as `calibre-debug src/cli.py -- <command> ...`.
"""
import os
import sys

USAGE = """\
calibre-utils — maintenance tools for a Calibre library.

Usage:
    calibre-utils <command> [options]
    calibre-utils <command> --help

Commands:
    dedup       Merge duplicate book records (quarantine + manifest)
    fix-sort    Backfill author_sort and title sort fields"""


def _ensure_calibre():
    """Re-exec under calibre-debug if the calibre modules aren't importable."""
    try:
        import calibre  # noqa: F401
        return
    except ImportError:
        pass
    if os.environ.get("_CALIBRE_RELAUNCHED"):
        sys.exit("error: the 'calibre' modules are unavailable even under "
                 "calibre-debug — is Calibre installed?")
    os.environ["_CALIBRE_RELAUNCHED"] = "1"
    argv = ["calibre-debug", os.path.abspath(__file__), "--", *sys.argv[1:]]
    try:
        os.execvp("calibre-debug", argv)
    except FileNotFoundError:
        sys.exit("error: 'calibre-debug' not found on PATH — install Calibre.")


_ensure_calibre()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dedup
import fix_sort

COMMANDS = {
    "dedup": dedup.main,
    "fix-sort": fix_sort.main,
}


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0 if argv else 1
    cmd, rest = argv[0], argv[1:]
    entry = COMMANDS.get(cmd)
    if entry is None:
        sys.stderr.write(f"calibre-utils: unknown command {cmd!r}\n\n")
        print(USAGE)
        return 2
    entry(rest, prog=f"calibre-utils {cmd}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
