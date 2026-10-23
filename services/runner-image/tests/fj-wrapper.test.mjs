import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  chmodSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const wrapper = resolve(scriptDir, "..", "fj-wrapper");

/**
 * The wrapper runs in every agent container, in front of every forge call.
 *
 * It records the invocation and classifies nothing: which action a command
 * performed is decided by the runner, on the host, from what is written here.
 * So these tests pin two things and no more — that the agent's result is never
 * altered by the recording, and that the recording is faithful. What any of it
 * *means* is tested in services/runner/tests/test_forge_actions.py, against
 * recordings rather than against a live binary.
 *
 * `fj-bin` is faked. The real binary wraps interpolated values in Unicode bidi
 * isolates — its output is literally `created pull request #⁨7⁩` — so the fake
 * emits those too, and the bytes reach the recording unchanged.
 */
function sandbox(t) {
  const root = mkdtempSync(join(tmpdir(), "fj-wrapper-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const bin = join(root, "bin");
  mkdirSync(bin, { recursive: true });

  const fake = join(bin, "fj-bin");
  writeFileSync(
    fake,
    [
      "#!/bin/sh",
      '[ "$1" = "--host" ] && shift 2',
      'case "$1 $2" in',
      '  "auth add-token") exit 0 ;;',
      "  \"pr create\") { printf 'created pull request #\\342\\201\\250'; printf '7'; printf '\\342\\201\\251: t\\n'; } >&2; exit 0 ;;",
      "  \"pr merge\") printf 'merged\\n'; printf 'warned\\n' >&2; exit 0 ;;",
      "  \"issue view\") printf 'read output\\n'; exit 0 ;;",
      "  \"pr close\") printf 'boom\\n' >&2; exit 3 ;;",
      "esac",
      "exit 0",
    ].join("\n"),
  );
  chmodSync(fake, 0o755);

  const calls = join(root, "forge-calls");
  const run = (...args) =>
    spawnSync("sh", [wrapper, ...args], {
      encoding: "utf8",
      env: {
        ...process.env,
        PATH: `${bin}:${process.env.PATH}`,
        SWECRAFT_FORGE_URL: "http://forge:3400",
        SWECRAFT_FORGE_TOKEN: "token",
        SWECRAFT_AGENT: "rich-bot",
        SWECRAFT_FORGE_CALLS: calls,
      },
    });
  const records = () =>
    existsSync(calls)
      ? readdirSync(calls)
          .sort()
          .map((name) => JSON.parse(readFileSync(join(calls, name), "utf8")))
      : [];
  return { run, records, calls };
}

test("a read passes through untouched and records nothing", (t) => {
  const { run, records } = sandbox(t);
  const result = run("issue", "view", "hub/rich#1");

  assert.equal(result.status, 0);
  assert.equal(result.stdout, "read output\n");
  assert.deepEqual(records(), []);
});

test("a mutation is recorded as the invocation it was", (t) => {
  const { run, records } = sandbox(t);
  const result = run("pr", "create", "hub/rich", "title");

  // The agent still sees exactly what the CLI wrote, on the stream fj used.
  assert.equal(result.status, 0);
  assert.match(result.stderr, /created pull request/);
  assert.equal(result.stdout, "");

  const [record] = records();
  assert.equal(record.tool, "fj");
  assert.equal(record.actor, "rich-bot");
  assert.equal(record.exit, 0);
  assert.deepEqual(record.argv, ["pr", "create", "hub/rich", "title"]);
  // The bidi isolates survive, so the host decides what to do about them.
  assert.match(record.stderr, /created pull request #⁨7⁩/);
});

test("stdout and stderr stay separate for the agent and in the record", (t) => {
  const { run, records } = sandbox(t);
  const result = run("pr", "merge", "hub/rich#2");

  assert.equal(result.stdout, "merged\n");
  assert.equal(result.stderr, "warned\n");
  const [record] = records();
  assert.equal(record.stdout, "merged\n");
  assert.equal(record.stderr, "warned\n");
});

test("a failing command keeps its exit code and is recorded with it", (t) => {
  const { run, records } = sandbox(t);
  const result = run("pr", "close", "hub/rich");

  assert.equal(result.status, 3);
  assert.equal(result.stderr, "boom\n");
  // Recorded rather than dropped: the runner skips non-zero exits when it
  // derives the ledger, and a failed attempt is worth having in the recording.
  assert.equal(records()[0].exit, 3);
});

test("a subcommand nobody classified is captured rather than skipped", (t) => {
  // The wrapper's list is of reads, so an unfamiliar verb takes the recorded
  // path. Being wrong about one costs a buffered command; the old direction —
  // a list of known mutations — cost a missing record instead.
  const { run, records } = sandbox(t);
  const result = run("issue", "transfer", "hub/rich#9", "hub/other");

  assert.equal(result.status, 0);
  assert.deepEqual(records()[0].argv, [
    "issue",
    "transfer",
    "hub/rich#9",
    "hub/other",
  ]);
});

test("auth is never recorded, because its argument is the credential", (t) => {
  const { run, records } = sandbox(t);
  const result = run("auth", "add-token", "tok-abcdefgh");

  assert.equal(result.status, 0);
  assert.deepEqual(records(), []);
});

test("arguments survive quoting, spaces, and newlines", (t) => {
  const { run, records } = sandbox(t);
  const argv = ["issue", "comment", "hub/rich#1", "--body", 'a "quoted"\nbody'];
  run(...argv);

  assert.deepEqual(records()[0].argv, argv);
});

test("each call gets its own file, so concurrent writes cannot interleave", (t) => {
  const { run, records } = sandbox(t);
  run("pr", "merge", "hub/rich#1");
  run("pr", "merge", "hub/rich#2");

  assert.equal(records().length, 2);
});

test("output is capped, so one command cannot fill the artifact", (t) => {
  const root = mkdtempSync(join(tmpdir(), "fj-wrapper-cap-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const bin = join(root, "bin");
  mkdirSync(bin, { recursive: true });
  const fake = join(bin, "fj-bin");
  writeFileSync(fake, "#!/bin/sh\nhead -c 40000 /dev/zero | tr '\\0' 'x'\nexit 0\n");
  chmodSync(fake, 0o755);

  const calls = join(root, "forge-calls");
  const result = spawnSync("sh", [wrapper, "pr", "merge", "hub/rich#1"], {
    encoding: "utf8",
    env: {
      ...process.env,
      PATH: `${bin}:${process.env.PATH}`,
      SWECRAFT_FORGE_CALLS: calls,
      SWECRAFT_FORGE_CALL_MAX_BYTES: "128",
    },
  });

  // The agent still receives every byte; only the record is capped.
  assert.equal(result.stdout.length, 40000);
  const [record] = readdirSync(calls).map((name) =>
    JSON.parse(readFileSync(join(calls, name), "utf8")),
  );
  assert.equal(record.stdout.length, 128);
});

test("bookkeeping that cannot be written is never worth a wake", (t) => {
  const root = mkdtempSync(join(tmpdir(), "fj-wrapper-ro-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const bin = join(root, "bin");
  mkdirSync(bin, { recursive: true });
  const fake = join(bin, "fj-bin");
  writeFileSync(fake, "#!/bin/sh\nprintf 'created pull request #1\\n'\nexit 0\n");
  chmodSync(fake, 0o755);

  const blocked = join(root, "file");
  writeFileSync(blocked, "not a directory\n");
  const result = spawnSync("sh", [wrapper, "pr", "create", "hub/rich"], {
    encoding: "utf8",
    env: {
      ...process.env,
      PATH: `${bin}:${process.env.PATH}`,
      // A path whose parent is a regular file: the recording cannot be made.
      SWECRAFT_FORGE_CALLS: join(blocked, "forge-calls"),
    },
  });

  assert.equal(result.status, 0);
  assert.match(result.stdout, /created pull request/);
});

test("an explicit --host bypasses the wrapper entirely", (t) => {
  const { run, records } = sandbox(t);
  const result = run("--host", "http://elsewhere", "pr", "merge", "hub/rich#1");

  assert.equal(result.status, 0);
  assert.deepEqual(records(), []);
});

test("a body passed by file is recorded beside the command", (t) => {
  const { run, records, calls } = sandbox(t);
  const notes = join(dirname(calls), "notes.md");
  writeFileSync(notes, "profiled your parser; see hub/alpha#3\n");

  // Both spellings; the file's name stays in argv, its words land in `body`.
  assert.equal(run("issue", "comment", "hub/rich#1", "--body-file", notes).status, 0);
  assert.equal(run("issue", "comment", "hub/rich#1", `--body-file=${notes}`).status, 0);
  // A file that is not there is not an error for the agent, and no body.
  assert.equal(run("issue", "comment", "hub/rich#1", "--body-file", "/nope.md").status, 0);
  // An inline body has nothing to read.
  assert.equal(run("issue", "comment", "hub/rich#1", "--body", "inline").status, 0);

  // Records are named by mktemp, so find them by what they recorded.
  const all = records();
  const byFlag = all.find((r) => r.argv[3] === "--body-file" && r.argv[4] === notes);
  const byEquals = all.find((r) => r.argv[3] === `--body-file=${notes}`);
  const missing = all.find((r) => r.argv[4] === "/nope.md");
  const inline = all.find((r) => r.argv[3] === "--body");
  assert.equal(byFlag.body, "profiled your parser; see hub/alpha#3\n");
  assert.deepEqual(byFlag.argv, ["issue", "comment", "hub/rich#1", "--body-file", notes]);
  assert.equal(byEquals.body, "profiled your parser; see hub/alpha#3\n");
  assert.equal(missing.body, null);
  assert.equal(inline.body, null);
});
