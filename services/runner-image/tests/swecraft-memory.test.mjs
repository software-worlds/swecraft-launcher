import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const hubMemory = resolve(scriptDir, "..", "swecraft-memory");

function memoryTree(t) {
  const directory = mkdtempSync(join(tmpdir(), "swecraft-memory-"));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  return directory;
}

function writeFile(root, relativePath, content) {
  const target = join(root, relativePath);
  mkdirSync(dirname(target), { recursive: true });
  writeFileSync(target, content);
}

function run(memoryDir, args) {
  return spawnSync(process.execPath, [hubMemory, ...args], {
    encoding: "utf8",
    env: { ...process.env, SWECRAFT_MEMORY_DIR: memoryDir },
  });
}

function assertOk(result) {
  assert.equal(
    result.status,
    0,
    `expected success, got ${result.status}\nstdout:\n${result.stdout}\nstderr:\n${result.stderr}`
  );
}

test("search finds matching markdown, json, and jsonl memory files", (t) => {
  const memoryDir = memoryTree(t);
  writeFile(memoryDir, "PROJECT.md", "project needle\n");
  writeFile(memoryDir, "github/example/repo/issue-1.json", '{"title":"json needle"}\n');
  writeFile(memoryDir, "events/run.jsonl", '{"body":"jsonl needle"}\n');
  writeFile(memoryDir, "ignored.txt", "text needle\n");

  const result = run(memoryDir, ["search", "needle"]);

  assertOk(result);
  assert.match(result.stdout, /^PROJECT\.md:1: project needle$/m);
  assert.match(
    result.stdout,
    /^github\/example\/repo\/issue-1\.json:1: {"title":"json needle"}$/m
  );
  assert.match(result.stdout, /^events\/run\.jsonl:1: {"body":"jsonl needle"}$/m);
  assert.doesNotMatch(result.stdout, /ignored\.txt/);
});

test("search skips symlinks and files larger than the search limit", (t) => {
  const memoryDir = memoryTree(t);
  const outside = memoryTree(t);
  writeFile(outside, "linked.md", "hidden needle\n");
  symlinkSync(join(outside, "linked.md"), join(memoryDir, "linked-file.md"));
  symlinkSync(outside, join(memoryDir, "linked-dir"));
  writeFile(memoryDir, "oversized.md", `${"x".repeat(1024 * 1024)} needle\n`);

  const result = run(memoryDir, ["search", "needle"]);

  assertOk(result);
  assert.equal(result.stdout, "no matches\n");
});

test("add --section inserts under an existing section and creates a missing section", (t) => {
  const memoryDir = memoryTree(t);
  writeFile(
    memoryDir,
    "MEMORY.md",
    [
      "# Memory",
      "",
      "## Durable",
      "",
      "- existing durable",
      "",
      "## Other",
      "",
      "- existing other",
      "",
    ].join("\n")
  );

  const existing = run(memoryDir, ["add", "--section", "Durable", "new durable"]);
  assertOk(existing);
  const withExisting = readFileSync(join(memoryDir, "MEMORY.md"), "utf8");
  assert.match(
    withExisting,
    /## Durable\n\n- existing durable\n- new durable\n\n## Other/
  );

  const missing = run(memoryDir, ["add", "--section", "Open Questions", "new question"]);
  assertOk(missing);
  const withMissing = readFileSync(join(memoryDir, "MEMORY.md"), "utf8");
  assert.match(withMissing, /## Open Questions\n\n- new question\n$/);
});

test("daily creates the standard heading once and appends subsequent bullets", (t) => {
  const memoryDir = memoryTree(t);

  const first = run(memoryDir, ["daily", "first item"]);
  const second = run(memoryDir, ["daily", "second item"]);

  assertOk(first);
  assertOk(second);
  const dailyDir = join(memoryDir, "daily");
  const [dailyFile] = readdirSync(dailyDir);
  assert.match(dailyFile, /^\d{4}-\d{2}-\d{2}\.md$/);

  const date = dailyFile.slice(0, -".md".length);
  assert.equal(
    readFileSync(join(dailyDir, dailyFile), "utf8"),
    `# Daily log for ${date}\n\n## Activity\n\n- first item\n- second item\n`
  );
});

test("replace and remove edit exactly one bullet within a section", (t) => {
  const memoryDir = memoryTree(t);
  writeFile(
    memoryDir,
    "MEMORY.md",
    [
      "# Memory",
      "",
      "## Durable",
      "",
      "- stale durable fact",
      "",
      "## Other",
      "",
      "- stale other fact",
      "",
    ].join("\n")
  );

  const replaced = run(memoryDir, [
    "replace",
    "--section",
    "Durable",
    "--match",
    "stale",
    "--with",
    "current durable fact",
  ]);
  const removed = run(memoryDir, [
    "remove",
    "--section",
    "Other",
    "--match",
    "stale",
  ]);

  assertOk(replaced);
  assert.match(replaced.stdout, /replaced bullet/);
  assertOk(removed);
  assert.match(removed.stdout, /removed bullet/);
  const content = readFileSync(join(memoryDir, "MEMORY.md"), "utf8");
  assert.match(content, /## Durable\n\n- current durable fact/);
  assert.doesNotMatch(content, /stale durable fact|stale other fact/);
});

test("replace refuses ambiguous matches and lists candidates without editing", (t) => {
  const memoryDir = memoryTree(t);
  const original = [
    "# Memory",
    "",
    "## Durable",
    "",
    "- build with command alpha",
    "- build with command beta",
    "",
  ].join("\n");
  writeFile(memoryDir, "MEMORY.md", original);

  const result = run(memoryDir, [
    "replace",
    "--match",
    "build with command",
    "--with",
    "new build command",
  ]);

  assert.equal(result.status, 2);
  assert.match(result.stderr, /multiple bullets matched/);
  assert.match(result.stderr, /build with command alpha/);
  assert.match(result.stderr, /build with command beta/);
  assert.equal(readFileSync(join(memoryDir, "MEMORY.md"), "utf8"), original);
});

test("replace collapses duplicates and identical replacements are no-ops", (t) => {
  const memoryDir = memoryTree(t);
  writeFile(
    memoryDir,
    "MEMORY.md",
    "# Memory\n\n## Durable\n\n- stale fact\n- current fact\n"
  );

  const collapsed = run(memoryDir, [
    "replace",
    "--match",
    "stale fact",
    "--with",
    "current fact",
  ]);
  assertOk(collapsed);
  assert.match(collapsed.stdout, /collapsed duplicate/);
  const collapsedContent = readFileSync(join(memoryDir, "MEMORY.md"), "utf8");
  assert.equal(collapsedContent.match(/- current fact/g)?.length, 1);
  assert.doesNotMatch(collapsedContent, /stale fact/);

  const unchanged = run(memoryDir, [
    "replace",
    "--match",
    "current fact",
    "--with",
    "current fact",
  ]);
  assertOk(unchanged);
  assert.match(unchanged.stdout, /no change/);
  assert.equal(
    readFileSync(join(memoryDir, "MEMORY.md"), "utf8"),
    collapsedContent
  );
});

test("replace and remove report missing matches and sections", (t) => {
  const memoryDir = memoryTree(t);
  const original = "# Memory\n\n## Durable\n\n- known fact\n";
  writeFile(memoryDir, "MEMORY.md", original);

  const missingMatch = run(memoryDir, [
    "remove",
    "--match",
    "absent",
  ]);
  assert.equal(missingMatch.status, 2);
  assert.match(missingMatch.stderr, /no bullet matched/);

  const missingSection = run(memoryDir, [
    "replace",
    "--section",
    "Missing",
    "--match",
    "known",
    "--with",
    "updated",
  ]);
  assert.equal(missingSection.status, 2);
  assert.match(missingSection.stderr, /no section "Missing"/);
  assert.equal(readFileSync(join(memoryDir, "MEMORY.md"), "utf8"), original);
});

test("caller-fixable failures exit nonzero with usage or path errors", (t) => {
  const memoryDir = memoryTree(t);
  const missingDir = join(memoryDir, "missing");

  const missingDirectory = run(missingDir, ["show"]);
  assert.equal(missingDirectory.status, 2);
  assert.match(missingDirectory.stderr, /memory workspace not found/);

  const missingSearchQuery = run(memoryDir, ["search"]);
  assert.equal(missingSearchQuery.status, 2);
  assert.match(missingSearchQuery.stderr, /usage: swecraft-memory search <query>/);

  const missingSectionName = run(memoryDir, ["add", "--section"]);
  assert.equal(missingSectionName.status, 2);
  assert.match(missingSectionName.stderr, /--section requires a name/);
});
