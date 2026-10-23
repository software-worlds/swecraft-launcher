"""Tests for the git data and agents' details: branches, trees, blobs, histories, agents, and what is refused."""

import gzip
import hashlib
import io
import json
import lzma
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import repo_code
import repo_git
import synthetic
from redact import Redactor
from repo_git import branches, browser_order, build, homes, main, profile_homes, safe_path, write

# Synthetic values only.
SECRET_LINE = "The grammar loads in 40 ms"


def bare_repository(path: Path) -> Path:
    path.mkdir(parents=True)
    synthetic.git("init", "--bare", "-q", ".", cwd=path)
    return path


def read(path: Path) -> dict:
    return json.loads(path.read_text())


def binary_tree(repo: Path, files: dict[str, bytes]) -> str:
    """Store `files` (path → bytes) in the bare repository as a tree, as synthetic.write_tree does texts; the tree's id."""

    entries, folders = [], {}
    for path, data in files.items():
        name, _, rest = path.partition("/")
        if rest:
            folders.setdefault(name, {})[rest] = data
        else:
            entries.append(f"100644 blob {repo_code.git(repo, 'hash-object', '-w', '--stdin', stdin=data).decode().strip()}\t{name}")
    entries += [f"040000 tree {binary_tree(repo, inner)}\t{name}" for name, inner in folders.items()]
    return synthetic.git("mktree", cwd=repo, stdin="".join(f"{entry}\n" for entry in entries))


def binary_commit(repo: Path, files: dict[str, bytes]) -> None:
    """A commit of these contents, the head of `main`."""

    commit = synthetic.git("commit-tree", binary_tree(repo, files), "-m", "binary contents", cwd=repo)
    synthetic.git("update-ref", "refs/heads/main", commit, cwd=repo)
    synthetic.git("symbolic-ref", "HEAD", "refs/heads/main", cwd=repo)


def zip_of(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


class BranchOrderTest(unittest.TestCase):
    """The default branch first, then the others by their head commit's date, newest first."""

    @classmethod
    def setUpClass(cls):
        cls.scratch = tempfile.TemporaryDirectory()
        cls.repo = bare_repository(Path(cls.scratch.name) / "r.git")
        for branch, date in (
            ("main", "2026-08-01T10:00:00Z"),
            ("ci/tests", "2026-08-01T10:20:00Z"),
            ("perf/x", "2026-08-01T10:25:00Z"),
            ("agent/memory", "2026-08-01T10:29:00Z"),
            ("b2", "2026-08-01T10:05:00Z"),
            ("b1", "2026-08-01T10:05:00Z"),
        ):
            sha = synthetic.commit(cls.repo, {"README.md": f"{branch}\n"}, f"on {branch}", date=date)
            synthetic.git("update-ref", f"refs/heads/{branch}", sha, cwd=cls.repo)
        synthetic.git("symbolic-ref", "HEAD", "refs/heads/main", cwd=cls.repo)

    @classmethod
    def tearDownClass(cls):
        cls.scratch.cleanup()

    def test_the_default_first_then_newest_first(self):
        default, found = branches(self.repo, "main", Redactor())
        # Neither name order: dates decide, and two branches with one date are in name order.
        self.assertEqual([b["name"] for b in found], ["main", "agent/memory", "perf/x", "ci/tests", "b1", "b2"])
        self.assertEqual(default, "main")
        self.assertEqual(found[1], {"name": "agent/memory", "head": found[1]["head"], "date": "2026-08-01T10:29:00Z", "subject": "on agent/memory", "author": "alpha-bot"})

    def test_a_default_the_repository_lacks(self):
        # The repository's HEAD stands in, then the newest branch.
        self.assertEqual(branches(self.repo, "trunk", Redactor())[0], "main")
        synthetic.git("symbolic-ref", "HEAD", "refs/heads/gone", cwd=self.repo)
        try:
            default, found = branches(self.repo, None, Redactor())
        finally:
            synthetic.git("symbolic-ref", "HEAD", "refs/heads/main", cwd=self.repo)
        self.assertEqual((default, found[0]["name"]), ("agent/memory", "agent/memory"))


class ExportTest(unittest.TestCase):
    """The synthetic society's git data, as the exporter writes it."""

    @classmethod
    def setUpClass(cls):
        cls.scratch = tempfile.TemporaryDirectory()
        root = Path(cls.scratch.name)
        cls.report = synthetic.export(root / "inputs", root / "bundles")
        cls.tiny = root / "bundles" / "tiny"
        cls.repo = read(cls.tiny / "git" / "alpha.json")
        cls.trees = {b["name"]: read(cls.tiny / "git" / "alpha" / "trees" / f"{b['head']}.json") for b in cls.repo["branches"]}

    @classmethod
    def tearDownClass(cls):
        cls.scratch.cleanup()

    def entry(self, branch: str, path: str) -> dict:
        return next(e for e in self.trees[branch]["entries"] if e["path"] == path)

    def blob(self, branch: str, path: str) -> dict:
        return read(self.tiny / "blobs" / f"{self.entry(branch, path)['blob']}.json")

    def test_branches(self):
        self.assertEqual(self.repo["defaultBranch"], "main")
        self.assertEqual([b["name"] for b in self.repo["branches"]], ["main", "release/1.0", "agent/memory", "perf/cache-grammar"])

    def test_entry_kinds(self):
        kinds = {e["path"]: (e["kind"], e["mode"], e["size"], e["blob"] is None) for e in self.trees["perf/cache-grammar"]["entries"]}
        self.assertEqual(kinds["alpha/parser.py"][:2], ("file", "100644"))
        self.assertEqual(kinds["docs/README.md"][:2], ("link", "120000"))
        self.assertEqual(kinds["vendor/grammar"], ("submodule", "160000", None, True))
        self.assertEqual(self.blob("perf/cache-grammar", "docs/README.md")["text"]["text"], "../README.md")

    def test_paths_in_the_browsers_order(self):
        for tree in self.trees.values():
            paths = [e["path"] for e in tree["entries"]]
            self.assertEqual(paths, sorted(paths, key=browser_order))

    def test_a_blob_two_branches_share_is_written_once(self):
        shared = self.entry("main", "README.md")["blob"]
        self.assertEqual(self.entry("perf/cache-grammar", "README.md")["blob"], shared)
        named = {e["blob"] for tree in self.trees.values() for e in tree["entries"] if e["blob"]}
        self.assertEqual(sorted(p.stem for p in (self.tiny / "blobs").glob("*.json")), sorted(named))
        self.assertEqual(self.report["git bytes"]["blobs"]["files"], len(named))

    def test_a_binary_contents_bytes_are_written_unmodified(self):
        logo = self.blob("main", "logo.png")
        self.assertEqual((logo["binary"], logo["text"], logo["bytes"]), (True, None, "exported"))
        data = (self.tiny / "blobs" / f"{logo['id']}.bin").read_bytes()
        # Git's object id of the bytes is the file's name.
        self.assertEqual(hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest(), logo["id"])
        self.assertEqual(len(data), logo["size"])
        self.assertIsNone(self.blob("main", "README.md")["bytes"])

    def test_a_binary_content_with_a_secrets_pattern_is_withheld(self):
        compiled = self.blob("perf/cache-grammar", "alpha/__pycache__/settings.cpython-312.pyc")
        self.assertEqual((compiled["binary"], compiled["text"], compiled["bytes"]), (True, None, "withheld"))
        self.assertFalse((self.tiny / "blobs" / f"{compiled['id']}.bin").exists())
        self.assertEqual(sorted(p.name for p in (self.tiny / "blobs").glob("*.bin")), [f"{self.entry('main', 'logo.png')['blob']}.bin"])

    def test_a_secret_inside_a_file_is_redacted(self):
        settings = self.blob("main", "alpha/settings.py")
        self.assertEqual(settings["text"], {"text": 'TOKEN = "[redacted]"\n', "redacted": 1, "truncated": False})
        for path in (self.tiny / "blobs").iterdir():
            self.assertNotIn(synthetic.FAKE_GITHUB_TOKEN.encode(), path.read_bytes())

    def test_the_memory_pointer_of_a_maintained_home(self):
        memory = next(b["head"] for b in self.repo["branches"] if b["name"] == "agent/memory")
        alpha = read(self.tiny / "agents" / "alpha-bot.json")
        self.assertEqual(alpha["memory"], {"repo": "alpha", "branch": "agent/memory", "head": memory})
        self.assertEqual(alpha["instructions"]["text"], synthetic.ALPHA_CHARTER)
        # beta has no repository in the forge, and no charter.
        beta = read(self.tiny / "agents" / "beta-bot.json")
        self.assertEqual((beta["memory"], beta["instructions"]), ({"repo": "beta", "branch": "agent/memory", "head": None}, None))

    def test_the_report(self):
        git = self.report["git"]
        self.assertEqual((git["repositories"], git["branches"], git["distinct commits"], git["agents"]), (1, 4, 4, 3))
        # The link and the submodule are in two trees: perf/cache-grammar's and the release branch's merge of it.
        self.assertEqual((git["links"], git["submodules"], git["binary blobs"]), (2, 2, 2))
        self.assertEqual((git["binary blobs withheld"], git["binary blobs withheld, github"]), (1, 1))
        self.assertEqual((git["logs"], git["commits"]), (1, 5))
        self.assertEqual(self.report["git bytes"]["binary bytes"]["files"], 1)
        for kind in ("branches", "trees", "blobs", "agents", "logs", "commits", "binary bytes"):
            sizes = self.report["git bytes"][kind]
            self.assertGreater(sizes["total"], 0)
            self.assertGreater(sizes["gzipped"], 0)


class HistoryTest(unittest.TestCase):
    """alpha's commits since the society started: its log, and a file per commit."""

    MERGE = "Merge branch 'perf/cache-grammar' into release/1.0"

    @classmethod
    def setUpClass(cls):
        cls.scratch = tempfile.TemporaryDirectory()
        root = Path(cls.scratch.name)
        synthetic.export(root / "inputs", root / "bundles")
        cls.tiny = root / "bundles" / "tiny"
        cls.seed = next(r["seed"] for r in read(cls.tiny / "core.json")["repos"] if r["name"] == "alpha")
        cls.log = read(cls.tiny / "git" / "alpha" / "log.json")
        cls.listed = {c["subject"]: c for c in cls.log["commits"]}

    @classmethod
    def tearDownClass(cls):
        cls.scratch.cleanup()

    def commit(self, subject: str) -> dict:
        return read(self.tiny / "git" / "alpha" / "commits" / f"{self.listed[subject]['id']}.json")

    def test_commits_before_the_seed_are_left_out(self):
        self.assertEqual((self.log["repo"], self.log["seed"]), ("alpha", self.seed))
        # Upstream's release 1.0 is the seed's parent; release 1.1 is reachable only from the tag v1.1.
        self.assertNotIn(self.seed, [c["id"] for c in self.log["commits"]])
        self.assertFalse(any(c["subject"].startswith("Upstream:") for c in self.log["commits"]))
        files = sorted(p.stem for p in (self.tiny / "git" / "alpha" / "commits").iterdir())
        self.assertEqual(files, sorted(c["id"] for c in self.log["commits"]))

    def test_newest_first_each_before_its_parents(self):
        self.assertEqual(
            [(c["date"], c["subject"]) for c in self.log["commits"]],
            [
                ("2026-08-01T10:35:00Z", self.MERGE),
                ("2026-08-01T10:29:00Z", "alpha-bot: notes"),
                ("2026-08-01T10:20:00Z", "perf: bound the cache"),
                ("2026-08-01T10:15:00Z", "perf: keep the grammar between calls"),
                ("2026-08-01T10:09:00Z", "perf: cache the grammar"),
            ],
        )
        listed = set()
        for c in self.log["commits"]:
            self.assertFalse(listed & set(c["parents"]), c["subject"])
            listed.add(c["id"])

    def test_parents_and_branches(self):
        head, perf = self.listed["perf: cache the grammar"], self.listed["perf: keep the grammar between calls"]
        self.assertEqual(self.listed[self.MERGE]["parents"], [head["id"], perf["id"]])
        self.assertEqual(head["parents"], [self.seed])
        self.assertEqual(self.listed["alpha-bot: notes"]["parents"], [])
        # A branch is marked on its head; the head of a pull request whose branch was deleted has none.
        self.assertEqual(
            {c["subject"]: c["branches"] for c in self.log["commits"]},
            {
                self.MERGE: ["release/1.0"],
                "alpha-bot: notes": ["agent/memory"],
                "perf: bound the cache": [],
                "perf: keep the grammar between calls": ["perf/cache-grammar"],
                "perf: cache the grammar": ["main"],
            },
        )
        self.assertEqual(
            {k: head[k] for k in ("author", "authorEmail", "authored")},
            {"author": "alpha-bot", "authorEmail": "alpha-bot@forge.invalid", "authored": "2026-08-01T10:09:00Z"},
        )

    def test_a_commit_file_repeats_its_log_entry(self):
        for listed in self.log["commits"]:
            detail = self.commit(listed["subject"])
            self.assertEqual(detail["repo"], "alpha")
            for key in ("id", "parents", "author", "authorEmail", "date", "authored"):
                self.assertEqual(detail[key], listed[key], key)
            self.assertEqual(detail["message"]["text"].split("\n", 1)[0], listed["subject"])

    def test_the_files_of_a_commit(self):
        files = {f["path"]: f for f in self.commit("perf: cache the grammar")["files"]}
        self.assertEqual(sorted(files), ["alpha/compat.py", "alpha/parser.py", "alpha/scratch.py", "alpha/settings.py", "logo.png"])
        self.assertEqual(
            files["alpha/compat.py"],
            {"path": "alpha/compat.py", "from": "alpha/legacy.py", "status": "renamed", "additions": 0, "deletions": 0, "binary": False, "diff": None},
        )
        parser = files["alpha/parser.py"]
        self.assertEqual((parser["status"], parser["additions"], parser["deletions"]), ("modified", 4, 1))
        self.assertTrue(parser["diff"]["text"].startswith("@@ -1,3 +1,6 @@\n+_GRAMMAR = None\n"))
        self.assertEqual((files["alpha/scratch.py"]["status"], files["alpha/scratch.py"]["deletions"]), ("deleted", 1))
        self.assertEqual({k: files["logo.png"][k] for k in ("binary", "additions", "deletions", "diff")}, {"binary": True, "additions": None, "deletions": None, "diff": None})
        self.assertEqual(files["alpha/settings.py"]["diff"], {"text": '@@ -0,0 +1 @@\n+TOKEN = "[redacted]"', "redacted": 1, "truncated": False})

    def test_a_merge_lists_its_changes_against_its_first_parent(self):
        merge = self.commit(self.MERGE)
        added = ["alpha/__pycache__/settings.cpython-312.pyc", "alpha/cache.py", "docs/README.md", "vendor/grammar"]
        self.assertEqual([(f["path"], f["status"]) for f in merge["files"]], [(path, "added") for path in added])
        self.assertEqual(merge["files"], self.commit("perf: keep the grammar between calls")["files"])

    def test_a_commit_without_parents_lists_every_file_as_added(self):
        notes = self.commit("alpha-bot: notes")
        self.assertEqual([(f["path"], f["status"], f["additions"]) for f in notes["files"]], [("MEMORY.md", "added", 3), ("notebook.md", "added", 1)])

    def test_a_secret_in_a_commit_message_is_redacted(self):
        message = self.commit(self.MERGE)["message"]
        self.assertEqual(message, {"text": synthetic.ALPHA_MERGE_MESSAGE.replace(synthetic.FAKE_GITHUB_TOKEN, "[redacted]"), "redacted": 1, "truncated": False})
        for path in (self.tiny / "git").rglob("*.json"):
            self.assertNotIn(synthetic.FAKE_GITHUB_TOKEN, path.read_text())

    def test_a_repository_whose_seed_the_forge_lacks_has_no_log(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            synthetic.export(root / "inputs", root / "bundles")
            core = read(root / "bundles" / "tiny" / "core.json")
            next(r for r in core["repos"] if r["name"] == "alpha")["seed"] = "0" * 40
            report = build(root / "out", core, root / "inputs" / "analysis", root / "inputs" / "checkpoint", Redactor())
            self.assertTrue((root / "out" / "git" / "alpha.json").is_file())
            self.assertEqual(sorted(p.name for p in (root / "out" / "git" / "alpha").iterdir()), ["trees"])
            self.assertEqual((report["git"]["seeds not in the forge"], report["git"].get("logs")), (1, None))


def write_profiles(checkpoint: Path, entries: str) -> None:
    """The coordinator's configuration in a checkpoint's apparatus, with these agent entries."""

    (checkpoint / "apparatus" / "coordinator.yaml").write_text(f"workers: 4\nagents:\n{entries}checkpoint_every_wakes: 90\n")


class ProfileHomesTest(unittest.TestCase):
    """The homes the coordinator's agent profiles name, read from its configuration."""

    def homes_in(self, text: str) -> dict:
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "coordinator.yaml"
            path.write_text(text)
            return profile_homes(path)

    def test_both_yaml_forms(self):
        text = (
            "agents:\n"
            "  - name: alpha-bot        # forge username\n"
            '    home_repo: "alpha-bot/home"\n'
            "    write_repos: [hub/alpha]\n"
            "    agent_env:\n"
            "      name: not-the-agent\n"
            "      home_repo: not/this\n"
            "  - {name: gamma-bot, home_repo: hub/gamma, write_repos: [hub/gamma], agent_env: {X: env:Y}}  # in the world\n"
            "  - {name: beta-bot, repo: hub/beta}\n"
            "workers: 4\n"
        )
        self.assertEqual(self.homes_in(text), {"alpha-bot": "alpha-bot/home", "gamma-bot": "hub/gamma"})

    def test_the_legacy_form_names_no_home(self):
        # As in experiments 6 to 8: each agent's home is the repository it maintains.
        self.assertEqual(self.homes_in("agents:\n  - {name: attrs-bot, repo: hub/attrs, agent_env: {X: env:Y}}\n"), {})
        self.assertEqual(profile_homes(Path("/nonexistent/coordinator.yaml")), {})

    def test_a_home_in_the_worlds_organisation_is_keyed_by_its_name(self):
        with tempfile.TemporaryDirectory() as scratch:
            checkpoint = Path(scratch)
            (checkpoint / "apparatus").mkdir()
            write_profiles(checkpoint, "  - {name: alpha-bot, home_repo: alpha-bot/home}\n  - {name: beta-bot, home_repo: hub/gamma}\n")
            core = {"world": {"org": "hub"}, "agents": [{"name": n, "repo": r} for n, r in (("alpha-bot", "alpha"), ("beta-bot", "beta"), ("gamma-bot", "gamma"))]}
            self.assertEqual(homes(core, checkpoint), {"alpha-bot": "alpha-bot/home", "beta-bot": "gamma", "gamma-bot": "gamma"})


class DedicatedHomeTest(unittest.TestCase):
    """An agent's home in both layouts of the coordinator's profiles: the repository it maintains, or its own."""

    def test_the_memory_pointer_with_profiles_in_the_legacy_form(self):
        # As experiments 6 to 8 write them: `name` and `repo`, no `home_repo`.
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            synthetic.export(root / "inputs", root / "bundles")
            core = read(root / "bundles" / "tiny" / "core.json")
            evidence, checkpoint = root / "inputs" / "analysis", root / "inputs" / "checkpoint"
            write_profiles(checkpoint, "  - {name: alpha-bot, repo: hub/alpha, agent_env: {X_TOKEN: env:Y}}\n  - {name: beta-bot, repo: hub/beta}\n")
            report = build(root / "out", core, evidence, checkpoint, Redactor())
            memory = next(b["head"] for b in read(root / "out" / "git" / "alpha.json")["branches"] if b["name"] == "agent/memory")
            self.assertEqual(read(root / "out" / "agents" / "alpha-bot.json")["memory"], {"repo": "alpha", "branch": "agent/memory", "head": memory})
            self.assertEqual(sorted(p.name for p in (root / "out" / "git").iterdir()), ["alpha", "alpha.json"])
            self.assertEqual(report["git"]["repositories"], 1)

    def test_the_memory_pointer_of_a_dedicated_home(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            synthetic.export(root / "inputs", root / "bundles")
            core = read(root / "bundles" / "tiny" / "core.json")
            evidence, checkpoint = root / "inputs" / "analysis", root / "inputs" / "checkpoint"
            home = bare_repository(evidence / "forge" / "git" / "repositories" / "alpha-bot" / "home.git")
            notes = synthetic.commit(home, {"MEMORY.md": "notes at home\n"}, "alpha-bot: notes at home", date="2026-08-01T10:40:00Z")
            synthetic.git("update-ref", "refs/heads/agent/memory", notes, cwd=home)
            synthetic.git("symbolic-ref", "HEAD", "refs/heads/agent/memory", cwd=home)
            write_profiles(checkpoint, "  - name: alpha-bot\n    home_repo: alpha-bot/home\n    write_repos: [hub/alpha]\n")
            report = build(root / "out", core, evidence, checkpoint, Redactor())
            alpha = read(root / "out" / "agents" / "alpha-bot.json")
            self.assertEqual(alpha["memory"], {"repo": "alpha-bot/home", "branch": "agent/memory", "head": notes})
            # The home's git data is written too, its files named with `~` in place of `/`.
            branches_file = read(root / "out" / "git" / "alpha-bot~home.json")
            self.assertEqual((branches_file["repo"], branches_file["defaultBranch"]), ("alpha-bot/home", "agent/memory"))
            self.assertTrue((root / "out" / "git" / "alpha-bot~home" / "trees" / f"{notes}.json").is_file())
            self.assertEqual(report["git"]["repositories"], 2)
            # A home is made during the run and has no seed: its log holds its whole history.
            log = read(root / "out" / "git" / "alpha-bot~home" / "log.json")
            self.assertEqual((log["repo"], log["seed"]), ("alpha-bot/home", None))
            self.assertEqual([(c["id"], c["parents"], c["branches"]) for c in log["commits"]], [(notes, [], ["agent/memory"])])
            commit = read(root / "out" / "git" / "alpha-bot~home" / "commits" / f"{notes}.json")
            self.assertEqual((commit["repo"], [f["path"] for f in commit["files"]]), ("alpha-bot/home", ["MEMORY.md"]))
            self.assertEqual(report["git"]["logs"], 2)


class RefusedTest(unittest.TestCase):
    """Paths and names that could not be a plain relative path or a file's name."""

    def test_safe_paths(self):
        for path in ("README.md", "a/b/c.py", ".git/config", "docs/.hidden", "naïve.txt"):
            self.assertTrue(safe_path(path), path)
        for path in ("", "/etc/passwd", "a/", "a//b", "./a", "a/./b", "../a", "a/../b", "..", "a\x01b", "a\nb"):
            self.assertFalse(safe_path(path), repr(path))

    def test_unsafe_tree_paths_are_refused(self):
        with tempfile.TemporaryDirectory() as scratch:
            repo = bare_repository(Path(scratch) / "r.git")
            blob = synthetic.git("hash-object", "-w", "--stdin", cwd=repo, stdin="x\n")
            inner = synthetic.git("mktree", cwd=repo, stdin=f"100644 blob {blob}\tx\n")
            # ls-tree lists them as "ok.txt", ".", "a\x01b" and "../x".
            names = [("100644 blob", blob, "ok.txt"), ("100644 blob", blob, "."), ("100644 blob", blob, "a\x01b"), ("040000 tree", inner, "..")]
            tree = synthetic.git("mktree", cwd=repo, stdin="".join(f"{kind} {oid}\t{name}\n" for kind, oid, name in names))
            commit = synthetic.git("commit-tree", tree, "-m", "unsafe names", cwd=repo)
            store = repo_git.Store(Path(scratch) / "out", Redactor())
            entries = store.tree(repo, commit)
            self.assertEqual([e["path"] for e in entries], ["ok.txt"])
            self.assertEqual(store.counts["paths refused"], 3)

    def test_unsafe_agent_and_home_names_are_refused(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            synthetic.export(root / "inputs", root / "bundles")
            core = read(root / "bundles" / "tiny" / "core.json")
            core["agents"].append({**core["agents"][0], "name": "../../state/coordinator/api_token"})
            write_profiles(root / "inputs" / "checkpoint", "  - {name: beta-bot, home_repo: ../evil, write_repos: [hub/beta]}\n")
            report = build(root / "out", core, root / "inputs" / "analysis", root / "inputs" / "checkpoint", Redactor())
            self.assertEqual(sorted(p.name for p in (root / "out" / "agents").iterdir()), ["alpha-bot.json", "beta-bot.json", "gamma-bot.json"])
            self.assertEqual((report["git"]["agents refused"], report["git"]["homes refused"]), (1, 1))
            self.assertIsNone(read(root / "out" / "agents" / "beta-bot.json")["memory"])


class BinaryKnownSecretTest(unittest.TestCase):
    """A binary content that holds a known secret: its bytes are withheld, as the redactor cannot rewrite them."""

    def test_withheld_and_counted(self):
        with tempfile.TemporaryDirectory() as scratch:
            repo = bare_repository(Path(scratch) / "r.git")
            held = synthetic.git("hash-object", "-w", "--stdin", cwd=repo, stdin=f"\0\x01{SECRET_LINE}\x02")
            plain = synthetic.git("hash-object", "-w", "--stdin", cwd=repo, stdin="\0\x01plain bytes\x02")
            store = repo_git.Store(Path(scratch) / "out", Redactor([SECRET_LINE]))
            store.blobs(repo, [held, plain])
            self.assertEqual(read(Path(scratch) / "out" / "blobs" / f"{held}.json")["bytes"], "withheld")
            self.assertEqual(read(Path(scratch) / "out" / "blobs" / f"{plain}.json")["bytes"], "exported")
            self.assertEqual(sorted(p.name for p in (Path(scratch) / "out" / "blobs").glob("*.bin")), [f"{plain}.bin"])
            self.assertEqual((store.counts["binary blobs withheld"], store.counts["binary blobs withheld, known"]), (1, 1))
            for path in (Path(scratch) / "out").rglob("*"):
                if path.is_file():
                    self.assertNotIn(SECRET_LINE.encode(), path.read_bytes())


class CompressedTest(unittest.TestCase):
    """A compressed binary content is searched in what it expands to: xz, gzip, bzip2 and zip."""

    def export(self, files: dict[str, bytes], known: tuple[str, ...] = ()) -> tuple[Path, dict, dict]:
        """The blobs folder, the report, and each path's blob, after exporting a repository of these files."""

        scratch = Path(self.enterContext(tempfile.TemporaryDirectory()))
        repo = bare_repository(scratch / "r.git")
        binary_commit(repo, files)
        store = repo_git.Store(scratch / "out", Redactor(known))
        heads = store.repository("r", repo, "main", seed=None, log=False)
        entries = read(scratch / "out" / "git" / "r" / "trees" / f"{heads['main']}.json")["entries"]
        blobs = {e["path"]: read(scratch / "out" / "blobs" / f"{e['blob']}.json") for e in entries}
        return scratch / "out" / "blobs", store.report(), blobs

    def test_a_known_secret_inside_an_xz_file_is_withheld(self):
        folder, report, blobs = self.export(
            {"data/held.xz": lzma.compress(f"payload: {SECRET_LINE}\n".encode()), "data/plain.xz": lzma.compress(b"payload\n")}, (SECRET_LINE,)
        )
        self.assertEqual({path: blob["bytes"] for path, blob in blobs.items()}, {"data/held.xz": "withheld", "data/plain.xz": "exported"})
        self.assertEqual(report["withheld"], [{"repo": "r", "path": "data/held.xz", "id": blobs["data/held.xz"]["id"], "found": "known"}])
        self.assertEqual((report["unscanned"], report["git"]["binary blobs expanded"]), ([], 2))
        self.assertEqual([p.name for p in folder.glob("*.bin")], [f"{blobs['data/plain.xz']['id']}.bin"])

    def test_a_token_in_a_zip_member_and_in_a_gzip_inside_a_zip(self):
        token = synthetic.FAKE_GITHUB_TOKEN
        _, report, blobs = self.export(
            {
                "dist/member.whl": zip_of({"alpha/settings.py": f'TOKEN = "{token}"\n'.encode()}),
                "dist/nested.zip": zip_of({"notes.gz": gzip.compress(f"token {token}\n".encode())}),
                "dist/clean.zip": zip_of({"notes.gz": gzip.compress(b"no secret here\n")}),
            }
        )
        self.assertEqual({path: blob["bytes"] for path, blob in blobs.items()}, {"dist/member.whl": "withheld", "dist/nested.zip": "withheld", "dist/clean.zip": "exported"})
        self.assertEqual([(item["path"], item["found"]) for item in report["withheld"]], [("dist/member.whl", "github"), ("dist/nested.zip", "github")])

    def test_a_file_past_the_limit_is_exported_and_listed_unscanned(self):
        with mock.patch.object(repo_git, "EXPANDED_LIMIT", 1000):
            folder, report, blobs = self.export({"big.gz": gzip.compress(b"\0" * 5000), "small.gz": gzip.compress(b"\0" * 500)})
        big = blobs["big.gz"]
        self.assertEqual((big["bytes"], blobs["small.gz"]["bytes"]), ("exported", "exported"))
        self.assertEqual(report["unscanned"], [{"repo": "r", "path": "big.gz", "id": big["id"], "why": ["gzip: past the expanded size limit"]}])
        self.assertEqual(report["git"]["binary blobs unscanned"], 1)
        self.assertTrue((folder / f"{big['id']}.bin").is_file())

    def test_what_cannot_be_read_is_listed_by_repository_and_path(self):
        locked = bytearray(zip_of({"secret.txt": b"stored as if encrypted"}))
        # The central directory's flag that marks a member as encrypted.
        locked[locked.index(b"PK\x01\x02") + 8] |= 0x1
        _, report, _ = self.export({"a/locked.zip": bytes(locked), "b/cut.gz": gzip.compress(b"x" * 100)[:-6], "c/frame.zst": b"\x28\xb5\x2f\xfd\0\0"})
        self.assertEqual(
            [(item["repo"], item["path"], item["why"]) for item in report["unscanned"]],
            [
                ("r", "a/locked.zip", ["zip: a member is encrypted"]),
                ("r", "b/cut.gz", ["gzip: damaged"]),
                ("r", "c/frame.zst", ["zstd, a format the standard library does not read"]),
            ],
        )
        self.assertEqual(report["withheld"], [])


class WriteTest(unittest.TestCase):
    """The git data written alone, into a run exported before."""

    def test_only_the_git_files_are_replaced(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            synthetic.export(root / "inputs", root / "bundles")
            run = root / "bundles" / "tiny"
            mine = {p.relative_to(run): p.read_bytes() for name in repo_git.OWNED for p in (run / name).rglob("*.json")}
            others = {p.relative_to(run): p.read_bytes() for p in run.rglob("*.json") if p.relative_to(run).parts[0] not in repo_git.OWNED}
            (run / "evaluation.json").write_text("{}")
            for name in repo_git.OWNED:
                shutil.rmtree(run / name)
                (run / name).mkdir()
                (run / name / "stale.json").write_text("{}")
            report = write(run, root / "inputs" / "analysis", root / "inputs" / "checkpoint", Redactor())
            self.assertEqual({p.relative_to(run): p.read_bytes() for name in repo_git.OWNED for p in (run / name).rglob("*.json")}, mine)
            self.assertEqual(
                {p.relative_to(run): p.read_bytes() for p in run.rglob("*.json") if p.relative_to(run).parts[0] not in repo_git.OWNED and p.name != "evaluation.json"},
                others,
            )
            self.assertEqual((run / "evaluation.json").read_text(), "{}")
            self.assertEqual((report["git"]["branches"], report["git"]["commits"]), (4, 5))

    def test_known_secrets(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            synthetic.export(root / "inputs", root / "bundles")
            run, analysis, checkpoint = root / "bundles" / "tiny", root / "inputs" / "analysis", root / "inputs" / "checkpoint"
            outside = root / "known"
            outside.write_text(SECRET_LINE + "\n")
            arguments = ["--evidence", str(analysis), "--checkpoint", str(checkpoint), "--out", str(run)]
            with mock.patch("builtins.print"):
                main([*arguments, "--known-secrets", str(outside)])
            memory = next(b["head"] for b in read(run / "git" / "alpha.json")["branches"] if b["name"] == "agent/memory")
            entry = next(e for e in read(run / "git" / "alpha" / "trees" / f"{memory}.json")["entries"] if e["path"] == "MEMORY.md")
            self.assertEqual(read(run / "blobs" / f"{entry['blob']}.json")["text"]["text"], "# Memory\n\n- [redacted]; cache it.\n")
            for path in run.rglob("*.json"):
                self.assertNotIn(SECRET_LINE, path.read_text())
            for folder in (checkpoint, analysis):
                inside = folder / "known"
                inside.write_text(SECRET_LINE + "\n")
                with self.subTest(folder=folder.name), mock.patch("sys.stderr"), self.assertRaises(SystemExit):
                    main([*arguments, "--known-secrets", str(inside)])

    def test_a_run_not_yet_exported(self):
        with tempfile.TemporaryDirectory() as scratch, mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            main(["--evidence", scratch, "--checkpoint", scratch, "--out", scratch])


if __name__ == "__main__":
    unittest.main()
