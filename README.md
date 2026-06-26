# calibre-utils

Maintenance tools for a [Calibre](https://calibre-ebook.com/) e-book library,
exposed as a single CLI:

```
calibre-utils dedup      # merge duplicate book records
calibre-utils fix-sort   # backfill author_sort and title sort fields
```

The commands operate on the library through Calibre's own Python API, so their
results match what Calibre itself would produce.

## Requirements

- **Calibre** installed, with `calibre-debug` on your `PATH` (it ships with
  Calibre). The tools run inside Calibre's bundled Python — no separate Python
  environment or dependencies to install.
- Close the **Calibre GUI** before running anything with `--apply`. The tools
  write to the library database directly.

## Install

There is nothing to build. Clone the repo and run `./calibre-utils`, or put it
on your `PATH`:

```sh
ln -s "$PWD/calibre-utils" ~/.local/bin/calibre-utils
```

The `calibre-utils` launcher is a small shell script that runs `src/cli.py`
under `calibre-debug`. You can also invoke the real entry point directly:

```sh
calibre-debug src/cli.py -- dedup --apply
```

## Usage

Every command **defaults to a dry-run** and only changes the library when you
pass `--apply`. Run a command with `--help` for its full option list.

The library path defaults to `~/Calibre Library`; override it with
`--library-path <path>` on any command.

### `calibre-utils dedup`

Finds books that share the same title and author and folds each duplicate group
into a single record.

- **Matching** — books are grouped by Calibre's own `title_sort` / `author_sort`
  forms, normalized to be case-, accent- and punctuation-insensitive (so
  `"The Hobbit"`, `"hobbit, the"` and `"The Hobbit!"` collapse together).
- **Survivor** — the record kept is the one with the most complete metadata
  (tie-break: most formats → largest total file size → lowest id).
- **Formats** — different formats are combined onto the survivor; for the same
  format, the **larger file wins** (a smaller copy may be truncated).
- **Metadata** — empty fields on the survivor are filled from the duplicates,
  and `tags` / `identifiers` / `languages` are unioned. Existing non-empty
  values on the survivor are never overwritten.
- **Quarantine, not delete** — duplicates are **not** hard-deleted. Each
  duplicate's on-disk book folder is first copied into the duplicates directory,
  mirroring the library layout, and only then is the record removed:

  ```
  <library>/Author/Title (42)/book.epub  ->  <dup-dir>/Author/Title (42)/book.epub
  ```

- **Audit trail** — on `--apply`, every removal is appended to
  `<dup-dir>/quarantine_manifest.csv` with columns:
  `title, author, calibre_id, survivor_id, original_path, new_path`.

Options:

| Option | Description |
| --- | --- |
| `--library-path <path>` | Path to the Calibre library (default: `~/Calibre Library`). |
| `--duplicates-dir <path>` | Where to quarantine removed duplicate folders (default: `./Duplicates`). |
| `--apply` | Perform the merge. Without this flag, runs as a dry-run. |
| `--limit <n>` | Scan only the first N books (useful for testing). |

Example:

```sh
# Preview what would happen
calibre-utils dedup --duplicates-dir ~/calibre-dupes

# Perform the merge (Calibre GUI must be closed)
calibre-utils dedup --duplicates-dir ~/calibre-dupes --apply
```

### `calibre-utils fix-sort`

Recomputes every book's `author_sort` and title sort using Calibre's own
algorithms and writes them back in a single transaction. Useful for cleaning up
records imported with missing or inconsistent sort fields.

Options:

| Option | Description |
| --- | --- |
| `--library-path <path>` | Path to the Calibre library (default: `~/Calibre Library`). |
| `--apply` | Write changes. Without this flag, runs as a dry-run. |
| `--limit <n>` | Process only the first N books (useful for testing). |

Example:

```sh
calibre-utils fix-sort           # preview
calibre-utils fix-sort --apply   # write (Calibre GUI must be closed)
```

## How it works

`calibre-utils` is a thin shell launcher that runs the Python entry point
(`src/cli.py`) inside Calibre's interpreter via `calibre-debug`. The dispatcher
routes to per-command modules (`src/dedup.py`, `src/fix_sort.py`), each of which
talks to the library through Calibre's `new_api` — reading metadata, computing
sort keys, merging formats, and writing changes in-process. Each command module
can also be run on its own (e.g. `./src/dedup.py --apply`).

## Safety notes

- Commands are **dry-run by default**; nothing changes without `--apply`.
- **Close the Calibre GUI** before applying changes.
- `dedup` never hard-deletes: removed duplicates are copied into the duplicates
  directory and recorded in `quarantine_manifest.csv` first. Back up your
  library if you want extra assurance.
```
