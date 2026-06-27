#!/usr/bin/env python3
"""calibre-dedup — merge duplicate book records.

Finds books that share the same title and author (matched on Calibre's own
title_sort / author_sort forms, case- and punctuation-insensitive) and folds
each duplicate group into a single record:

  - different formats are combined onto one book
  - for the same format, the larger file wins (the smaller may be truncated)
  - metadata is combined: empty fields on the survivor are filled from the
    duplicates, and tags / identifiers / languages are unioned

The survivor is the record with the most complete metadata (tie-break: most
formats, then largest total size, then lowest id).

Duplicates are NOT hard-deleted. Each duplicate's on-disk book folder is first
copied into the duplicates directory, mirroring the library's layout
(e.g. "<library>/Author/Title (42)/book.epub" -> "<dup-dir>/Author/Title (42)/
book.epub"), and only then is the record removed from the Calibre library.
On --apply, each removal is appended to <dup-dir>/quarantine_manifest.csv
(title, author, calibre_id, survivor_id, original_path, new_path).

This must run inside Calibre's bundled Python (it imports calibre.*). When
launched with a normal interpreter it re-executes itself under calibre-debug,
so any of these work:

    ./src/dedup.py --apply
    calibre-debug src/dedup.py -- --apply
"""
import argparse
import csv
import os
import re
import shutil
import sys
import unicodedata


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
from calibre.utils.date import UNDEFINED_DATE

DEFAULT_LIBRARY = os.path.join(os.path.expanduser("~"), "Calibre Library")

# Fields that count toward a record's "completeness" when picking the survivor.
SCORE_FIELDS = ["tags", "series", "comments", "publisher", "pubdate",
                "rating", "languages", "identifiers"]


def parse_args(argv=None, prog="calibre-dedup"):
    p = argparse.ArgumentParser(
        prog=prog,
        description="Merge duplicate book records, quarantining the removed "
                    "duplicates' files instead of hard-deleting them.",
    )
    p.add_argument("--library-path", default=DEFAULT_LIBRARY,
                   help="Path to Calibre library (default: ~/Calibre Library)")
    p.add_argument("--duplicates-dir", default="Duplicates",
                   help="Where to quarantine removed duplicate folders "
                        "(default: ./Duplicates)")
    p.add_argument("--apply", action="store_true",
                   help="Perform the merge. Without this flag, runs as a dry-run. "
                        "Writes directly to the library, so close the Calibre GUI first.")
    p.add_argument("--limit", type=int,
                   help="Scan only the first N books (useful for testing)")
    return p.parse_args(argv)


def norm(s):
    """Normalize a string for matching: fold case and accents and drop
    punctuation/symbols, but KEEP letters and digits of every script.

    The previous version stripped everything outside ``[a-z0-9]``, which erased
    titles/authors written entirely in non-Latin scripts (Devanagari, Urdu, …)
    to the empty string — collapsing every such book into one bogus duplicate
    group. Here we keep any Unicode letter/mark/number and replace the rest with
    spaces, so distinct non-Latin titles stay distinct."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = "".join(c if (c.isspace() or unicodedata.category(c)[0] in "LMN") else " "
                for c in s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def fmt_size(n):
    units = ["B", "KB", "MB", "GB"]
    v = float(n)
    i = 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024
        i += 1
    return f"{v:.1f} {units[i]}"


def is_filled(field, val):
    if val is None:
        return False
    if field == "pubdate":
        return val != UNDEFINED_DATE
    if field == "rating":
        return bool(val)
    if isinstance(val, (list, tuple, dict, set)):
        return len(val) > 0
    if isinstance(val, str):
        return val.strip() != ""
    return True


def completeness(lib, bid):
    return sum(1 for f in SCORE_FIELDS if is_filled(f, lib.field_for(f, bid)))


def fmt_sizes(lib, bid):
    out = {}
    for fmt in lib.formats(bid):
        fm = lib.format_metadata(bid, fmt)
        out[fmt] = (fm or {}).get("size", 0)
    return out


def total_size(lib, bid):
    return sum(fmt_sizes(lib, bid).values())


def authors_str(lib, bid):
    return " & ".join(lib.field_for("authors", bid) or ())


def book_dir(lib, library_path, bid):
    for fmt in lib.formats(bid):
        p = lib.format_abspath(bid, fmt)
        if p:
            return os.path.dirname(p)
    rel = lib.field_for("path", bid)
    if rel:
        return os.path.join(library_path, rel.replace("/", os.sep))
    return None


def build_plan(lib, library_path, dup_root, book_ids):
    groups = {}
    skipped = []
    for bid in book_ids:
        title = lib.field_for("title", bid) or ""
        authors = list(lib.field_for("authors", bid) or ())
        title_key = norm(title_sort(title))
        author_key = norm(authors_to_sort_string(authors))
        # Never group a book whose title AND author yield no usable key — that
        # would lump together unrelated books with empty/symbol-only metadata.
        # Surface these instead so they can be reviewed by hand.
        if not title_key and not author_key:
            skipped.append({"id": bid, "title": title, "authors": " & ".join(authors)})
            continue
        key = title_key + " | " + author_key
        groups.setdefault(key, []).append(bid)

    plan_groups = []
    for key, ids in groups.items():
        if len(ids) < 2:
            continue

        survivor = max(ids, key=lambda b: (completeness(lib, b), len(lib.formats(b)),
                                           total_size(lib, b), -b))
        dups = [i for i in ids if i != survivor]

        # Formats: keep the largest file per format across the whole group.
        best = {}  # fmt -> (bid, size)
        for b in ids:
            for fmt, size in fmt_sizes(lib, b).items():
                if fmt not in best or size > best[fmt][1]:
                    best[fmt] = (b, size)
        survivor_formats = fmt_sizes(lib, survivor)
        format_actions = []
        for fmt in sorted(best):
            b, size = best[fmt]
            if fmt in survivor_formats:
                if b != survivor and size > survivor_formats[fmt]:
                    format_actions.append({"fmt": fmt, "action": "replace", "from_id": b,
                                           "size": size, "old_size": survivor_formats[fmt]})
            else:
                format_actions.append({"fmt": fmt, "action": "add", "from_id": b, "size": size})

        # Metadata: fill blank single-valued fields, union the multi-valued ones.
        set_ops = {}
        metadata_fills = []

        for field in ["comments", "publisher", "series", "rating", "author_sort"]:
            if not is_filled(field, lib.field_for(field, survivor)):
                for d in dups:
                    v = lib.field_for(field, d)
                    if is_filled(field, v):
                        set_ops[field] = v
                        if field == "series":
                            set_ops["series_index"] = lib.field_for("series_index", d)
                        metadata_fills.append({"field": field, "from_id": d})
                        break

        if not is_filled("pubdate", lib.field_for("pubdate", survivor)):
            for d in dups:
                v = lib.field_for("pubdate", d)
                if is_filled("pubdate", v):
                    set_ops["pubdate"] = v
                    metadata_fills.append({"field": "pubdate", "from_id": d})
                    break

        for field in ["tags", "languages"]:
            cur = list(lib.field_for(field, survivor) or ())
            seen = {x.lower() for x in cur}
            added = []
            for d in dups:
                for x in (lib.field_for(field, d) or ()):
                    if x.lower() not in seen:
                        seen.add(x.lower())
                        cur.append(x)
                        added.append(x)
            if added:
                set_ops[field] = tuple(cur)
                metadata_fills.append({"field": field, "added": added})

        cur_ids = dict(lib.field_for("identifiers", survivor) or {})
        added_ids = {}
        for d in dups:
            for k, v in (lib.field_for("identifiers", d) or {}).items():
                if k not in cur_ids:
                    cur_ids[k] = v
                    added_ids[k] = v
        if added_ids:
            set_ops["identifiers"] = cur_ids
            metadata_fills.append({"field": "identifiers", "added": added_ids})

        # Quarantine: each duplicate's folder is copied under dup_root, mirroring
        # the library's relative layout, before the record is removed.
        quarantine = []
        for d in dups:
            src = book_dir(lib, library_path, d)
            dest = os.path.join(dup_root, os.path.relpath(src, library_path)) if src else None
            quarantine.append({"id": d, "src": src, "dest": dest})

        plan_groups.append({
            "survivor": {"id": survivor, "title": lib.field_for("title", survivor) or "",
                         "authors": authors_str(lib, survivor),
                         "completeness": completeness(lib, survivor)},
            "duplicates": [{"id": d, "title": lib.field_for("title", d) or "",
                            "authors": authors_str(lib, d)} for d in dups],
            "format_actions": format_actions,
            "metadata_fills": metadata_fills,
            "quarantine": quarantine,
            "delete": dups,
            "set_ops": set_ops,
        })

    summary = {
        "groups": len(plan_groups),
        "books_deleted": sum(len(g["delete"]) for g in plan_groups),
        "formats_added": sum(1 for g in plan_groups for fa in g["format_actions"]
                             if fa["action"] == "add"),
        "formats_replaced": sum(1 for g in plan_groups for fa in g["format_actions"]
                                if fa["action"] == "replace"),
        "skipped": len(skipped),
    }
    return plan_groups, summary, skipped


def print_plan(plan_groups, summary, skipped, mode):
    for g in plan_groups:
        sv = g["survivor"]
        print(f"Group: {sv['title']} — {sv['authors']}")
        print(f"  survivor:   id={sv['id']} (completeness {sv['completeness']}/8)")
        print("  duplicates: " + ", ".join(f"id={d['id']}" for d in g["duplicates"]))

        if g["format_actions"]:
            print("  formats:")
            for fa in g["format_actions"]:
                if fa["action"] == "add":
                    print(f"    + {fa['fmt']:<5} from id={fa['from_id']}  "
                          f"({fmt_size(fa['size'])})  [survivor had none]")
                else:
                    print(f"    ~ {fa['fmt']:<5} from id={fa['from_id']}  "
                          f"({fmt_size(fa['size'])}, was {fmt_size(fa['old_size'])})")

        if g["metadata_fills"]:
            print("  metadata:")
            for m in g["metadata_fills"]:
                added = m.get("added")
                if isinstance(added, list):
                    print(f"    {m['field']:<11} + " + ", ".join(added))
                elif isinstance(added, dict):
                    print(f"    {m['field']:<11} + "
                          + ", ".join(f"{k}:{v}" for k, v in added.items()))
                else:
                    print(f"    {m['field']:<11} <- id={m['from_id']}")

        verb = "quarantined" if mode == "apply" else "will quarantine"
        print("  quarantine:")
        for q in g["quarantine"]:
            if q["dest"]:
                print(f"    id={q['id']} -> {q['dest']}")
            else:
                print(f"    id={q['id']} -> (no on-disk folder found; record removed only)")
        print(f"  -> {verb} & removed " + ", ".join(f"id={d}" for d in g["delete"]))
        print()

    if skipped:
        print("Skipped (no usable title/author key — review manually):")
        for b in skipped:
            print(f"  id={b['id']}  {b['title']!r} — {b['authors']!r}")
        print()

    s = summary
    removed_word = "removed" if mode == "apply" else "to remove"
    print(f"Summary: {s['groups']} duplicate group(s), {s['books_deleted']} book(s) "
          f"{removed_word}, {s['formats_added']} format(s) added, "
          f"{s['formats_replaced']} replaced, {s['skipped']} skipped.")


def execute(lib, plan_groups, dup_root):
    """Perform the merge, appending to the quarantine manifest as each group
    completes so the audit trail survives an interrupted run. Returns the
    manifest path, or None if there was nothing to remove."""
    if not any(g["delete"] for g in plan_groups):
        return None

    os.makedirs(dup_root, exist_ok=True)
    manifest = os.path.join(dup_root, "quarantine_manifest.csv")
    is_new = not os.path.exists(manifest)
    with open(manifest, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(["title", "author", "calibre_id", "survivor_id",
                        "original_path", "new_path"])
            f.flush()

        for g in plan_groups:
            survivor = g["survivor"]["id"]
            dup_info = {d["id"]: d for d in g["duplicates"]}

            # Copy formats onto the survivor before anything is removed.
            for fa in g["format_actions"]:
                src = lib.format_abspath(fa["from_id"], fa["fmt"])
                if src:
                    lib.add_format(survivor, fa["fmt"], src, replace=True, run_hooks=False)

            for field, val in g["set_ops"].items():
                lib.set_field(field, {survivor: val})

            # Quarantine each duplicate's folder (the copy is the backup that
            # makes the library removal safe to do permanently).
            rows = []
            for q in g["quarantine"]:
                if q["src"] and q["dest"] and os.path.isdir(q["src"]):
                    os.makedirs(os.path.dirname(q["dest"]), exist_ok=True)
                    shutil.copytree(q["src"], q["dest"], dirs_exist_ok=True)
                info = dup_info.get(q["id"], {})
                rows.append([
                    info.get("title", ""), info.get("authors", ""),
                    q["id"], survivor, q["src"] or "", q["dest"] or "",
                ])

            # Record the quarantine and flush BEFORE removing the records, so an
            # interrupted run can never delete books without logging where their
            # backups went.
            if rows:
                w.writerows(rows)
                f.flush()

            if g["delete"]:
                lib.remove_books(set(g["delete"]), permanent=True)

    return manifest


def main(argv=None, prog="calibre-dedup"):
    args = parse_args(argv, prog)
    library_path = os.path.abspath(args.library_path)
    dup_root = os.path.abspath(args.duplicates_dir)
    print(f"Library:    {library_path}")
    print(f"Duplicates: {dup_root}")
    print(f"Mode:       {'APPLY' if args.apply else 'dry-run'}\n")

    lib = DB(library_path).new_api
    book_ids = sorted(lib.all_book_ids())
    if args.limit:
        book_ids = book_ids[:args.limit]

    plan_groups, summary, skipped = build_plan(lib, library_path, dup_root, book_ids)
    print(f"Scanned {len(book_ids)} books, found {len(plan_groups)} duplicate "
          f"group(s); {len(skipped)} skipped.\n")
    if not plan_groups and not skipped:
        print("No duplicates found — nothing to do.")
        return

    mode = "apply" if args.apply else "plan"
    print_plan(plan_groups, summary, skipped, mode)

    if args.apply:
        manifest = execute(lib, plan_groups, dup_root)
        if manifest:
            print(f"\nQuarantine manifest updated: {manifest}")
        print("Done.")
    else:
        print("\nDry-run complete. Re-run with --apply to perform the merge.")
        print("WARNING: close the Calibre GUI before running --apply.")


if __name__ == "__main__":
    main()
