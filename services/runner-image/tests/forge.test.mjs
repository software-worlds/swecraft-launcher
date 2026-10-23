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
const forge = resolve(scriptDir, "..", "forge");

/**
 * `forge` is the surface `fj` does not cover — chiefly PR reviews — so it
 * carries the cross-agent activity most worth attributing to a wake.
 *
 * As with the fj wrapper, this records the request and interprets none of it.
 * What a method and path mean is tested in
 * services/runner/tests/test_forge_actions.py.
 */
function sandbox(t) {
  const root = mkdtempSync(join(tmpdir(), "forge-wrapper-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const bin = join(root, "bin");
  mkdirSync(bin, { recursive: true });

  const curl = join(bin, "curl");
  writeFileSync(
    curl,
    [
      "#!/bin/sh",
      'for a in "$@"; do',
      '  case "$a" in',
      "    *\"/fails/\"*) printf 'boom\\n' >&2; exit 22 ;;",
      "  esac",
      "done",
      "printf '{\"id\":812,\"number\":3}\\n'",
      "exit 0",
    ].join("\n"),
  );
  chmodSync(curl, 0o755);

  const calls = join(root, "forge-calls");
  const run = (...args) =>
    spawnSync("sh", [forge, ...args], {
      encoding: "utf8",
      env: {
        ...process.env,
        PATH: `${bin}:${process.env.PATH}`,
        SWECRAFT_FORGE_URL: "http://forge:3400",
        SWECRAFT_FORGE_TOKEN: "tok-abcdefgh",
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
  return { run, records };
}

test("a GET keeps the read fast path and records nothing", (t) => {
  const { run, records } = sandbox(t);
  const result = run("GET", "/repos/hub/rich/issues/7");

  assert.equal(result.status, 0);
  assert.deepEqual(records(), []);
});

test("a write is recorded with its method, path, and arguments", (t) => {
  const { run, records } = sandbox(t);
  const result = run(
    "POST",
    "/repos/hub/rich/issues/3/comments",
    "-d",
    '{"body":"hi"}',
  );

  assert.equal(result.status, 0);
  const [record] = records();
  assert.equal(record.tool, "forge");
  assert.equal(record.exit, 0);
  assert.deepEqual(record.argv, [
    "POST",
    "/repos/hub/rich/issues/3/comments",
    "-d",
    '{"body":"hi"}',
  ]);
  // The response is kept, because identity for a created object is only in it.
  assert.match(record.stdout, /"id":812/);
});

test("the token the wrapper injects is never part of the record", (t) => {
  // `argv` is what the agent passed, so the wrapper's own Authorization header
  // does not reach the file. A header the agent adds itself does, and is
  // redacted on the host against the run's secret list.
  const { run, records } = sandbox(t);
  run("POST", "/repos/hub/rich/issues", "-d", '{"title":"x"}');

  assert.equal(JSON.stringify(records()[0]).includes("tok-abcdefgh"), false);
});

test("a failed request keeps curl's exit code and is recorded with it", (t) => {
  const { run, records } = sandbox(t);
  const result = run("POST", "/repos/hub/fails/issues");

  assert.equal(result.status, 22);
  assert.equal(result.stderr, "boom\n");
  assert.equal(records()[0].exit, 22);
});

test("a missing token still fails before anything is recorded", (t) => {
  const root = mkdtempSync(join(tmpdir(), "forge-wrapper-notoken-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const calls = join(root, "forge-calls");
  const result = spawnSync("sh", [forge, "POST", "/repos/hub/rich/issues"], {
    encoding: "utf8",
    env: {
      ...process.env,
      SWECRAFT_FORGE_TOKEN: "",
      SWECRAFT_FORGE_READ_TOKEN: "",
      SWECRAFT_FORGE_CALLS: calls,
    },
  });

  assert.equal(result.status, 2);
  assert.equal(existsSync(calls), false);
});

test("usage errors are unchanged", (t) => {
  const { run } = sandbox(t);
  assert.equal(run("POST").status, 2);
});

test("a request body sent from a file is recorded beside the request", (t) => {
  const { run, records } = sandbox(t);
  const dir = mkdtempSync(join(tmpdir(), "forge-body-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const payload = join(dir, "review.json");
  writeFileSync(payload, '{"body": "LGTM, ship it"}');

  assert.equal(run("POST", "/repos/hub/rich/pulls/3/reviews", "--data-binary", `@${payload}`).status, 0);
  assert.equal(run("POST", "/repos/hub/rich/pulls/3/reviews", `-d@${payload}`).status, 0);
  assert.equal(run("POST", "/repos/hub/rich/pulls/3/reviews", "-d", '{"body": "inline"}').status, 0);

  // Records are named by mktemp, so find them by what they recorded.
  const all = records();
  const binary = all.find((r) => r.argv[2] === "--data-binary");
  const short = all.find((r) => r.argv[2] === `-d@${payload}`);
  const inline = all.find((r) => r.argv[2] === "-d");
  assert.equal(binary.body, '{"body": "LGTM, ship it"}');
  assert.equal(short.body, '{"body": "LGTM, ship it"}');
  assert.equal(inline.body, null);
});
