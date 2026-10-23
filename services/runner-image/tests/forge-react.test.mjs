import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  chmodSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const forgeReact = resolve(scriptDir, "..", "forge-react");

function sandbox(t) {
  const root = mkdtempSync(join(tmpdir(), "forge-react-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const bin = join(root, "bin");
  mkdirSync(bin, { recursive: true });
  const log = join(root, "forge.log");

  const forge = join(bin, "forge");
  writeFileSync(
    forge,
    [
      "#!/bin/sh",
      `printf '%s\\n' "$*" >> ${JSON.stringify(log)}`,
      'if [ "$1" = "GET" ] && [ "$2" = "/settings/ui" ]; then',
      '  printf \'{"allowed_reactions":["+1","-1","laugh","hooray","confused","heart","rocket","eyes"],"custom_emojis":["forgejo"]}\\n\'',
      "  exit 0",
      "fi",
      'printf \'[{"content":"eyes"}]\\n\'',
      "exit 0",
    ].join("\n"),
  );
  chmodSync(forge, 0o755);

  const run = (...args) =>
    spawnSync("sh", [forgeReact, ...args], {
      encoding: "utf8",
      env: {
        ...process.env,
        PATH: `${bin}:${process.env.PATH}`,
      },
    });
  const calls = () =>
    existsSync(log)
      ? readFileSync(log, "utf8")
          .trim()
          .split("\n")
          .filter(Boolean)
      : [];
  return { run, calls };
}

test("allowed reports the live forge allowlist", (t) => {
  const { run, calls } = sandbox(t);
  const result = run("allowed");

  assert.equal(result.status, 0);
  assert.deepEqual(JSON.parse(result.stdout), {
    allowed_reactions: [
      "+1",
      "-1",
      "laugh",
      "hooray",
      "confused",
      "heart",
      "rocket",
      "eyes",
    ],
    custom_emojis: ["forgejo"],
  });
  assert.deepEqual(calls(), ["GET /settings/ui"]);
});

test("list delegates issue and pull request threads to forge", (t) => {
  const { run, calls } = sandbox(t);
  const issue = run("list", "issue", "hub/rich#42");
  const pr = run("list", "pr", "hub/rich#7");

  assert.equal(issue.status, 0);
  assert.equal(pr.status, 0);
  assert.deepEqual(calls(), [
    "GET /repos/hub/rich/issues/42/reactions",
    "GET /repos/hub/rich/issues/7/reactions",
  ]);
});

test("add validates against the live allowlist and delegates the write", (t) => {
  const { run, calls } = sandbox(t);
  const result = run("add", "issue", "hub/rich#42", "eyes");

  assert.equal(result.status, 0);
  assert.deepEqual(calls(), [
    "GET /settings/ui",
    'POST /repos/hub/rich/issues/42/reactions -d {"content":"eyes"}',
  ]);
});

test("remove supports comment reactions and reactions that look like flags", (t) => {
  const { run, calls } = sandbox(t);
  const result = run("remove", "comment", "hub/rich", "812", "-1");

  assert.equal(result.status, 0);
  assert.deepEqual(calls(), [
    "GET /settings/ui",
    'DELETE /repos/hub/rich/issues/comments/812/reactions -d {"content":"-1"}',
  ]);
});

test("an invalid reaction is rejected before a mutation call", (t) => {
  const { run, calls } = sandbox(t);
  const result = run("add", "pr", "hub/rich#7", "shipit");

  assert.equal(result.status, 2);
  assert.match(result.stderr, /reaction 'shipit' is not allowed/);
  assert.deepEqual(calls(), ["GET /settings/ui"]);
});

test("invalid targets fail before consulting the forge", (t) => {
  const { run, calls } = sandbox(t);
  const result = run("add", "issue", "hub/rich", "eyes");

  assert.equal(result.status, 2);
  assert.match(result.stderr, /owner\/name#number/);
  assert.deepEqual(calls(), []);
});
