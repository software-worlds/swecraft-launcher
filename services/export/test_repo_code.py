"""The code file of a repository: a small bare repository with a seed and a head commit, built here."""

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import synthetic
from redact import Redactor
from repo_code import main, repo_code, write

CORE = [f"line {i}" for i in range(1, 11)]

SEED = {
    "README.md": "# demo\n",
    "pkg/core.py": "\n".join(CORE) + "\n",
    "pkg/old.py": "def old():\n    return 1\n",
    "pkg/gone.py": "x = 1\n",
    "pkg/run.sh": "echo hi\n",
    "logo.bin": "\0\1\2",
    "docs/guide.md": "# Guide\n",
}

HEAD_CORE = "\n".join([*CORE[:2], "line 3, changed", *CORE[3:], "line 11", "line 12"]) + "\n"
HEAD = {
    "README.md": "# demo\n",
    "pkg/core.py": HEAD_CORE,
    "pkg/new.py": "def old():\n    return 1\n",
    "pkg/run.sh": ("100755", "echo hi\n"),
    "pkg/settings.py": f'TOKEN = "{synthetic.FAKE_GITHUB_TOKEN}"\n',
    "logo.bin": "\0\1\2\3",
    "docs/guide.md": "# Guide\n",
    "docs/a file é.txt": "A path git quotes.\n",
    "big.txt": "y" * 500 + "\n",
}


class RepoCodeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = tempfile.TemporaryDirectory()
        cls.bare = Path(cls.scratch.name) / "demo.git"
        cls.bare.mkdir()
        synthetic.git("init", "--bare", "-q", ".", cwd=cls.bare)
        cls.seed = synthetic.commit(cls.bare, SEED, "seed")
        cls.head = synthetic.commit(cls.bare, HEAD, "work", cls.seed)
        cls.redactor = Redactor()
        cls.code = repo_code(cls.bare, cls.seed, cls.head, cls.redactor)
        cls.files = {file["path"]: file for file in cls.code["files"]}

    @classmethod
    def tearDownClass(cls):
        cls.scratch.cleanup()

    def test_every_changed_file_with_its_status(self):
        self.assertEqual((self.code["seed"], self.code["head"]), (self.seed, self.head))
        self.assertEqual(
            {path: file["status"] for path, file in self.files.items()},
            {
                "big.txt": "added",
                "docs/a file é.txt": "added",
                "logo.bin": "modified",
                "pkg/core.py": "modified",
                "pkg/gone.py": "deleted",
                "pkg/new.py": "renamed",
                "pkg/run.sh": "modified",
                "pkg/settings.py": "added",
            },
        )
        self.assertEqual(self.files["pkg/new.py"]["from"], "pkg/old.py")
        self.assertEqual({path for path, file in self.files.items() if file["from"]}, {"pkg/new.py"})
        self.assertEqual(list(self.code["files"][0]), ["path", "from", "status", "binary", "additions", "deletions", "size", "diff", "text", "omitted"])

    def test_line_counts_and_hunks(self):
        core = self.files["pkg/core.py"]
        self.assertEqual((core["additions"], core["deletions"]), (3, 1))
        diff = core["diff"]["text"]
        self.assertTrue(diff.startswith("@@ -1,6 +1,6 @@"), diff)
        self.assertIn("-line 3\n+line 3, changed\n", diff)
        self.assertTrue(diff.endswith("+line 11\n+line 12"))
        self.assertEqual(core["text"], {"text": HEAD_CORE, "redacted": 0, "truncated": False})
        self.assertEqual(core["size"], len(HEAD_CORE))

    def test_changes_without_lines(self):
        renamed, mode = self.files["pkg/new.py"], self.files["pkg/run.sh"]
        for file in (renamed, mode):
            self.assertEqual((file["additions"], file["deletions"], file["diff"]), (0, 0, None))
        self.assertEqual(renamed["text"]["text"], "def old():\n    return 1\n")

    def test_a_deleted_file_has_its_diff_and_no_text(self):
        gone = self.files["pkg/gone.py"]
        self.assertEqual((gone["additions"], gone["deletions"], gone["size"], gone["text"]), (0, 1, None, None))
        self.assertEqual(gone["diff"]["text"], "@@ -1 +0,0 @@\n-x = 1")

    def test_a_binary_file(self):
        logo = self.files["logo.bin"]
        self.assertEqual(
            {key: logo[key] for key in ("binary", "additions", "deletions", "size", "diff", "text")},
            {"binary": True, "additions": None, "deletions": None, "size": 4, "diff": None, "text": None},
        )
        self.assertFalse(any(file["binary"] for path, file in self.files.items() if path != "logo.bin"))

    def test_a_path_git_quotes(self):
        quoted = self.files["docs/a file é.txt"]
        self.assertEqual((quoted["additions"], quoted["text"]["text"]), (1, "A path git quotes.\n"))

    def test_secrets_are_redacted_in_the_diff_and_the_text(self):
        settings = self.files["pkg/settings.py"]
        for key in ("diff", "text"):
            self.assertEqual(settings[key]["redacted"], 1)
            self.assertIn("[redacted]", settings[key]["text"])
            self.assertNotIn(synthetic.FAKE_GITHUB_TOKEN, settings[key]["text"])

    def test_the_top_level_listing_directories_first(self):
        self.assertEqual(
            [(entry["name"], entry["kind"], entry["size"]) for entry in self.code["tree"]],
            [("docs", "dir", None), ("pkg", "dir", None), ("README.md", "file", 7), ("big.txt", "file", 501), ("logo.bin", "file", 4)],
        )


class TypeChangeTest(unittest.TestCase):
    """A file that became a link: one change in the raw diff, two blocks in the patch."""

    def test_one_file_with_both_blocks(self):
        with tempfile.TemporaryDirectory() as scratch:
            bare = Path(scratch) / "demo.git"
            bare.mkdir()
            synthetic.git("init", "--bare", "-q", ".", cwd=bare)
            seed = synthetic.commit(bare, {"README.md": "# demo\n", "docs/guide.md": "# Guide\nold\n"}, "seed")
            head = synthetic.commit(bare, {"README.md": "# demo\n", "docs/guide.md": ("120000", "../README.md")}, "a link", seed)
            files = repo_code(bare, seed, head, Redactor())["files"]
            self.assertEqual([(f["path"], f["status"], f["additions"], f["deletions"]) for f in files], [("docs/guide.md", "modified", 1, 2)])
            self.assertEqual(files[0]["diff"]["text"], "@@ -1,2 +0,0 @@\n-# Guide\n-old\n@@ -0,0 +1 @@\n+../README.md\n\\ No newline at end of file")
            self.assertEqual(files[0]["text"]["text"], "../README.md")


class LimitsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = tempfile.TemporaryDirectory()
        cls.bare = Path(cls.scratch.name) / "demo.git"
        cls.bare.mkdir()
        synthetic.git("init", "--bare", "-q", ".", cwd=cls.bare)
        cls.seed = synthetic.commit(cls.bare, SEED, "seed")
        cls.head = synthetic.commit(cls.bare, HEAD, "work", cls.seed)

    @classmethod
    def tearDownClass(cls):
        cls.scratch.cleanup()

    def test_no_bound_by_default(self):
        code = repo_code(self.bare, self.seed, self.head, Redactor())
        big = next(file for file in code["files"] if file["path"] == "big.txt")
        self.assertEqual(big["text"]["text"], HEAD["big.txt"])
        self.assertFalse(big["text"]["truncated"] or big["diff"]["truncated"] or big["omitted"])
        self.assertFalse(any(file["omitted"] for file in code["files"]))

    def test_each_text_is_cut_at_the_file_limit(self):
        code = repo_code(self.bare, self.seed, self.head, Redactor(), file_limit=100)
        big = next(file for file in code["files"] if file["path"] == "big.txt")
        for key in ("diff", "text"):
            self.assertTrue(big[key]["truncated"])
            self.assertTrue(big[key]["text"].endswith("characters cut]"))
        self.assertFalse(big["omitted"])

    def test_the_repository_limit_keeps_diffs_first(self):
        whole = repo_code(self.bare, self.seed, self.head, Redactor())
        diffs = sum(len(file["diff"]["text"]) for file in whole["files"] if file["diff"])
        code = repo_code(self.bare, self.seed, self.head, Redactor(), repo_limit=diffs + 20)
        # Every diff fits; of the texts, in path order, only one within the 20 characters left.
        self.assertTrue(all(file["diff"] for file in code["files"] if file["additions"] or file["deletions"]))
        texts = {file["path"]: file["text"]["text"] for file in code["files"] if file["text"]}
        self.assertEqual(texts, {"docs/a file é.txt": "A path git quotes.\n"})
        omitted = {file["path"] for file in code["files"] if file["omitted"]}
        self.assertEqual(omitted, {"big.txt", "pkg/core.py", "pkg/new.py", "pkg/run.sh", "pkg/settings.py"})

    def test_unknown_commits_and_a_missing_repository(self):
        empty = {"seed": None, "head": None, "tree": [], "files": []}
        self.assertEqual(repo_code(self.bare, "0" * 40, self.head, Redactor()), empty)
        self.assertEqual(repo_code(self.bare, self.seed, None, Redactor()), empty)
        self.assertEqual(repo_code(Path(self.scratch.name) / "missing.git", self.seed, self.head, Redactor()), empty)

    def test_no_change(self):
        code = repo_code(self.bare, self.head, self.head, Redactor())
        self.assertEqual((code["seed"], code["files"]), (self.head, []))
        self.assertEqual(len(code["tree"]), 5)


class WriteTest(unittest.TestCase):
    """A run's code files written alone, into a run exported before."""

    def test_only_the_code_files_are_replaced(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            synthetic.export(root / "inputs", root / "bundles")
            run = root / "bundles" / "tiny"
            code = {p.name: p.read_bytes() for p in (run / "code").iterdir()}
            others = {p.relative_to(run): p.read_bytes() for p in run.rglob("*.json") if p.relative_to(run).parts[0] != "code"}
            shutil.rmtree(run / "code")
            (run / "code").mkdir()
            (run / "code" / "stale.json").write_text("{}")
            report = write(run, root / "inputs" / "analysis", Redactor())
            self.assertEqual({p.name: p.read_bytes() for p in (run / "code").iterdir()}, code)
            self.assertEqual({p.relative_to(run): p.read_bytes() for p in run.rglob("*.json") if p.relative_to(run).parts[0] != "code"}, others)
            self.assertEqual(sorted(p.name for p in run.iterdir()), ["agents", "blobs", "code", "core.json", "git", "sessions", "threads"])
            self.assertEqual((report["code"]["changed files"], report["bytes"]["files"]), (5, 3))

    def test_a_run_not_yet_exported(self):
        with tempfile.TemporaryDirectory() as scratch, self.assertRaises(SystemExit):
            main(["--evidence", scratch, "--out", scratch])

    def test_known_secrets_reach_the_code_files(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            synthetic.export(root / "inputs", root / "bundles")
            run = root / "bundles" / "tiny"
            known = root / "known.txt"
            known.write_text("_GRAMMAR or load()\n")
            with contextlib.redirect_stdout(io.StringIO()):
                main(["--evidence", str(root / "inputs" / "analysis"), "--out", str(run), "--known-secrets", str(known)])
            alpha = json.loads((run / "code" / "alpha.json").read_text())
            text = next(f for f in alpha["files"] if f["path"] == "alpha/parser.py")["text"]["text"]
            self.assertIn("[redacted]", text)
            self.assertNotIn("_GRAMMAR or load()", text)

    def test_known_secrets_inside_the_analysis_are_refused(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            synthetic.export(root / "inputs", root / "bundles")
            known = root / "inputs" / "analysis" / "known.txt"
            known.write_text("abcdefgh12345\n")
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                main(["--evidence", str(root / "inputs" / "analysis"), "--out", str(root / "bundles" / "tiny"), "--known-secrets", str(known)])


if __name__ == "__main__":
    unittest.main()
