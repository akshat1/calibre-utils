#!/usr/bin/env -S npx tsx
import { spawn } from "node:child_process";
import { homedir } from "node:os";
import { join } from "node:path";

type Book = {
  id: number;
  title: string;
  authors: string;
  author_sort?: string;
};

type Plan = {
  id: number;
  title: string;
  authors: string;
  currentAuthorSort: string;
  desiredAuthorSort: string;
  desiredTitleSort: string;
};

type Args = {
  libraryPath: string;
  apply: boolean;
  bulk: boolean;
  limit?: number;
  concurrency: number;
};

function parseArgs(argv: string[]): Args {
  const args: Args = {
    libraryPath: join(homedir(), "Calibre Library"),
    apply: false,
    bulk: false,
    // calibredb takes an exclusive write lock per invocation, so >1 racing
    // workers fight each other and emit "another calibre program is running".
    concurrency: 1,
  };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "-h" || a === "--help") {
      printHelp();
      process.exit(0);
    } else if (a === "--apply") {
      args.apply = true;
    } else if (a === "--bulk") {
      args.bulk = true;
    } else if (a === "--library-path") {
      const v = argv[++i];
      if (!v) throw new Error("--library-path requires a value");
      args.libraryPath = v;
    } else if (a === "--limit") {
      const v = argv[++i];
      if (!v) throw new Error("--limit requires a value");
      args.limit = Number.parseInt(v, 10);
    } else if (a === "--concurrency") {
      const v = argv[++i];
      if (!v) throw new Error("--concurrency requires a value");
      args.concurrency = Math.max(1, Number.parseInt(v, 10));
    } else {
      throw new Error(`Unknown argument: ${a}`);
    }
  }
  return args;
}

function printHelp() {
  console.log(`calibre-fix-sort — backfill author_sort and title sort fields

Usage:
  tsx src/index.ts [options]

Options:
  --library-path <path>   Path to Calibre library (default: ~/Calibre Library)
  --apply                 Write changes. Without this flag, runs as dry-run.
  --bulk                  Write all updates in a single calibre-debug transaction
                          via LibraryDatabase.set_field. Much faster than the
                          default per-book calibredb path. Calibre GUI must be
                          closed; the write is atomic (all-or-nothing).
  --limit <n>             Process only the first N books (useful for testing)
  --concurrency <n>       Parallel calibredb set_metadata calls (default: 1).
                          Ignored when --bulk is set. Values >1 will fail —
                          calibredb locks the library per invocation.
  -h, --help              Show this help`);
}

function run(
  cmd: string,
  args: string[],
  opts: { stdin?: string } = {},
): Promise<{ stdout: string; stderr: string; code: number }> {
  return new Promise((resolve, reject) => {
    const child = spawn(cmd, args, { stdio: ["pipe", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (d) => (stdout += d.toString()));
    child.stderr.on("data", (d) => (stderr += d.toString()));
    child.on("error", reject);
    child.on("close", (code) => resolve({ stdout, stderr, code: code ?? 0 }));
    if (opts.stdin !== undefined) {
      child.stdin.end(opts.stdin);
    } else {
      child.stdin.end();
    }
  });
}

async function listBooks(libraryPath: string): Promise<Book[]> {
  const { stdout, stderr, code } = await run("calibredb", [
    "list",
    "--for-machine",
    "--fields=id,title,authors,author_sort",
    `--library-path=${libraryPath}`,
  ]);
  if (code !== 0) {
    throw new Error(`calibredb list failed (exit ${code}): ${stderr}`);
  }
  const parsed = JSON.parse(stdout) as Book[];
  return parsed;
}

// One-shot: feed all books to calibre-debug and let calibre's own algorithms
// produce author_sort and title_sort. Avoids per-book Python startup cost.
async function computeSorts(
  books: Book[],
): Promise<Map<number, { authorSort: string; titleSort: string }>> {
  const py = `
import sys, json
from calibre.ebooks.metadata import (
    string_to_authors,
    authors_to_sort_string,
    title_sort,
)

data = json.load(sys.stdin)
out = []
for row in data:
    authors_list = string_to_authors(row["authors"]) if row["authors"] else []
    out.append({
        "id": row["id"],
        "author_sort": authors_to_sort_string(authors_list) if authors_list else "",
        "title_sort": title_sort(row["title"] or ""),
    })
json.dump(out, sys.stdout)
`;
  const payload = JSON.stringify(
    books.map((b) => ({ id: b.id, title: b.title, authors: b.authors })),
  );
  const { stdout, stderr, code } = await run("calibre-debug", ["-c", py], {
    stdin: payload,
  });
  if (code !== 0) {
    throw new Error(`calibre-debug failed (exit ${code}): ${stderr}`);
  }
  const rows = JSON.parse(stdout) as Array<{
    id: number;
    author_sort: string;
    title_sort: string;
  }>;
  const map = new Map<number, { authorSort: string; titleSort: string }>();
  for (const r of rows) {
    map.set(r.id, { authorSort: r.author_sort, titleSort: r.title_sort });
  }
  return map;
}

function buildPlans(
  books: Book[],
  computed: Map<number, { authorSort: string; titleSort: string }>,
): Plan[] {
  const plans: Plan[] = [];
  for (const b of books) {
    const c = computed.get(b.id);
    if (!c) continue;
    plans.push({
      id: b.id,
      title: b.title,
      authors: b.authors,
      currentAuthorSort: (b.author_sort ?? "").trim(),
      desiredAuthorSort: c.authorSort,
      desiredTitleSort: c.titleSort,
    });
  }
  return plans;
}

function printDryRun(plans: Plan[]) {
  if (plans.length === 0) {
    console.log("Library is empty — nothing to do.");
    return;
  }
  console.log(`Will rewrite author_sort and title sort for ${plans.length} books:\n`);
  for (const p of plans) {
    console.log(`  id=${p.id}  ${p.title}`);
    console.log(`    authors:        ${p.authors}`);
    console.log(
      `    author_sort:    ${p.currentAuthorSort || "(empty)"}  →  ${p.desiredAuthorSort}`,
    );
    console.log(`    title sort:     →  ${p.desiredTitleSort}`);
    console.log("");
  }
  console.log(
    `Dry-run complete. Re-run with --apply to write these changes.`,
  );
}

async function applyPlan(
  plan: Plan,
  libraryPath: string,
): Promise<{ ok: boolean; error?: string }> {
  const { code, stderr } = await run("calibredb", [
    "set_metadata",
    `--library-path=${libraryPath}`,
    "--field",
    `author_sort:${plan.desiredAuthorSort}`,
    "--field",
    `sort:${plan.desiredTitleSort}`,
    String(plan.id),
  ]);
  if (code !== 0) return { ok: false, error: stderr.trim() || `exit ${code}` };
  return { ok: true };
}

async function applyBulk(plans: Plan[], libraryPath: string) {
  console.log(`Applying ${plans.length} updates in a single transaction...`);
  const py = `
import sys, json
from calibre.library import db

payload = json.load(sys.stdin)
lib = db(payload["library_path"]).new_api
author_sort_updates = {row["id"]: row["author_sort"] for row in payload["plans"]}
title_sort_updates = {row["id"]: row["title_sort"] for row in payload["plans"]}
changed_author = lib.set_field('author_sort', author_sort_updates)
changed_title = lib.set_field('sort', title_sort_updates)
json.dump({
    "changed_author_sort": len(changed_author),
    "changed_title_sort": len(changed_title),
    "queued": len(payload["plans"]),
}, sys.stdout)
`;
  const payload = JSON.stringify({
    library_path: libraryPath,
    plans: plans.map((p) => ({
      id: p.id,
      author_sort: p.desiredAuthorSort,
      title_sort: p.desiredTitleSort,
    })),
  });
  const { stdout, stderr, code } = await run("calibre-debug", ["-c", py], {
    stdin: payload,
  });
  if (code !== 0) {
    throw new Error(`calibre-debug bulk write failed (exit ${code}): ${stderr}`);
  }
  const result = JSON.parse(stdout) as {
    changed_author_sort: number;
    changed_title_sort: number;
    queued: number;
  };
  console.log(
    `Done. ${result.changed_author_sort}/${result.queued} author_sort and ${result.changed_title_sort}/${result.queued} title sort fields actually changed (others were already correct).`,
  );
}

async function applyAll(
  plans: Plan[],
  libraryPath: string,
  concurrency: number,
) {
  console.log(`Applying changes to ${plans.length} books (concurrency=${concurrency})...`);
  let done = 0;
  let failed = 0;
  const queue = [...plans];
  const workers: Promise<void>[] = [];
  for (let i = 0; i < concurrency; i++) {
    workers.push(
      (async () => {
        while (queue.length > 0) {
          const p = queue.shift();
          if (!p) break;
          const res = await applyPlan(p, libraryPath);
          done++;
          if (!res.ok) {
            failed++;
            console.error(`  FAIL id=${p.id} ${p.title}: ${res.error}`);
          }
          if (done % 25 === 0 || done === plans.length) {
            console.log(`  ${done}/${plans.length} processed`);
          }
        }
      })(),
    );
  }
  await Promise.all(workers);
  console.log(
    failed === 0
      ? `Done. ${done} books updated.`
      : `Done. ${done - failed} updated, ${failed} failed.`,
  );
  if (failed > 0) process.exitCode = 1;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const mode = args.apply
    ? args.bulk
      ? "APPLY (bulk)"
      : "APPLY"
    : "dry-run";
  console.log(`Library: ${args.libraryPath}`);
  console.log(`Mode:    ${mode}\n`);

  const allBooks = await listBooks(args.libraryPath);
  const books = args.limit ? allBooks.slice(0, args.limit) : allBooks;
  console.log(`Loaded ${books.length} books${args.limit ? ` (limited from ${allBooks.length})` : ""}.`);

  const computed = await computeSorts(books);
  const plans = buildPlans(books, computed);
  console.log(`${plans.length} books queued for rewrite.\n`);

  if (!args.apply) {
    printDryRun(plans);
    return;
  }
  if (plans.length === 0) return;
  if (args.bulk) {
    await applyBulk(plans, args.libraryPath);
  } else {
    await applyAll(plans, args.libraryPath, args.concurrency);
  }
}

main().catch((e) => {
  console.error(e instanceof Error ? e.message : String(e));
  process.exit(1);
});
