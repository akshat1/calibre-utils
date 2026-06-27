#!/usr/bin/env python3
"""calibre-fix-sort — backfill author_sort and title sort fields.

Recomputes every book's author_sort and title sort with Calibre's own
algorithms and writes them back in a single transaction. Runs as a dry-run
unless --apply is given.

This must run inside Calibre's bundled Python (it imports calibre.*). When
launched with a normal interpreter it re-executes itself under calibre-debug,
so any of these work:

    ./src/fix_sort.py --apply
    calibre-debug src/fix_sort.py -- --apply
"""
import argparse
import os
import sys


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

from calibre.library import db as DB
from calibre.ebooks.metadata import authors_to_sort_string, title_sort

DEFAULT_LIBRARY = os.path.join(os.path.expanduser("~"), "Calibre Library")


def parse_args(argv=None, prog="calibre-fix-sort"):
    p = argparse.ArgumentParser(
        prog=prog,
        description="Backfill author_sort and title sort fields using "
                    "Calibre's own sorting algorithms.",
    )
    p.add_argument("--library-path", default=DEFAULT_LIBRARY,
                   help="Path to Calibre library (default: ~/Calibre Library)")
    p.add_argument("--apply", action="store_true",
                   help="Write changes. Without this flag, runs as a dry-run. "
                        "Writes directly to the library, so close the Calibre GUI first.")
    p.add_argument("--limit", type=int,
                   help="Process only the first N books (useful for testing)")
    return p.parse_args(argv)


def build_plans(lib, book_ids):
    plans = []
    for bid in book_ids:
        title = lib.field_for("title", bid) or ""
        authors = list(lib.field_for("authors", bid) or ())
        plans.append({
            "id": bid,
            "title": title,
            "authors": " & ".join(authors),
            "current_author_sort": (lib.field_for("author_sort", bid) or "").strip(),
            "desired_author_sort": authors_to_sort_string(authors) if authors else "",
            "desired_title_sort": title_sort(title),
        })
    return plans


def print_dry_run(plans):
    print(f"Will rewrite author_sort and title sort for {len(plans)} books:\n")
    for p in plans:
        print(f"  id={p['id']}  {p['title']}")
        print(f"    authors:        {p['authors']}")
        cur = p["current_author_sort"] or "(empty)"
        print(f"    author_sort:    {cur}  ->  {p['desired_author_sort']}")
        print(f"    title sort:     ->  {p['desired_title_sort']}")
        print()
    print("Dry-run complete. Re-run with --apply to write these changes.")
    print("WARNING: close the Calibre GUI before running --apply.")


def apply_plans(lib, plans):
    print(f"Applying {len(plans)} updates in a single transaction...")
    author_updates = {p["id"]: p["desired_author_sort"] for p in plans}
    title_updates = {p["id"]: p["desired_title_sort"] for p in plans}
    changed_author = lib.set_field("author_sort", author_updates)
    changed_title = lib.set_field("sort", title_updates)
    print(f"Done. {len(changed_author)}/{len(plans)} author_sort and "
          f"{len(changed_title)}/{len(plans)} title sort fields actually "
          f"changed (others were already correct).")


def main(argv=None, prog="calibre-fix-sort"):
    args = parse_args(argv, prog)
    library_path = os.path.abspath(args.library_path)
    print(f"Library: {library_path}")
    print(f"Mode:    {'APPLY' if args.apply else 'dry-run'}\n")

    lib = DB(library_path).new_api
    book_ids = sorted(lib.all_book_ids())
    total = len(book_ids)
    if args.limit:
        book_ids = book_ids[:args.limit]
    print(f"Loaded {len(book_ids)} books"
          + (f" (limited from {total})" if args.limit else "") + ".")

    plans = build_plans(lib, book_ids)
    print(f"{len(plans)} books queued for rewrite.\n")
    if not plans:
        print("Library is empty — nothing to do.")
        return

    if args.apply:
        apply_plans(lib, plans)
    else:
        print_dry_run(plans)


if __name__ == "__main__":
    main()
