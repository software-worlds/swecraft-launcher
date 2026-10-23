from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import yaml

import swecraft_world.engine as engine_module
from swecraft_world import WorldError, load_lock, load_manifest
from swecraft_world.engine import ACTIONS_SETTLE_SECONDS, WorldEngine
from swecraft_world.models import RepositoryVisibility

SWECRAFT_COMMIT = "e" * 40
NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


def git(*arguments: str, cwd: Path) -> str:
    return subprocess.run(
        ("git", *arguments),
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def seed_upstream(
    root: Path, name: str, *, workflows: bool = True, workflows_readme: bool = False
) -> tuple[Path, str]:
    repository = root / "upstreams" / name
    repository.mkdir(parents=True)
    git("init", "--initial-branch=main", cwd=repository)
    git("config", "user.name", "Upstream", cwd=repository)
    git("config", "user.email", "upstream@example.test", cwd=repository)
    (repository / "README.md").write_text(f"# {name}\n", encoding="utf-8")
    if workflows:
        workflow = repository / ".github" / "workflows" / "ci.yml"
        workflow.parent.mkdir(parents=True)
        workflow.write_text("name: upstream\n", encoding="utf-8")
        if workflows_readme:
            (workflow.parent / "README.md").write_text(
                "# Upstream's own CI docs\n", encoding="utf-8"
            )
    git("add", ".", cwd=repository)
    git("commit", "-m", "upstream pin", cwd=repository)
    git("tag", "-a", "v1", "-m", "version one", cwd=repository)
    return repository, git("rev-parse", "HEAD", cwd=repository)


def write_manifest(root: Path, repos: list[dict]) -> Path:
    path = root / "world.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "world_id": "test-world",
                "org": "hub",
                "repos": repos,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def entry(name: str, upstream: Path, **extra: object) -> dict:
    return {
        "name": name,
        "upstream": str(upstream),
        "pin": {"type": "tag", "value": "v1"},
        **extra,
    }


class FakeForge:
    base_url = "http://forge.test"
    token = "admin-token"
    org = "hub"

    def __init__(self, root: Path) -> None:
        self.root = root / "forge"
        self.actions: dict[str, bool] = {}
        self.visibility: dict[str, RepositoryVisibility] = {}
        self.action_tasks: dict[str, list[dict]] = {}
        # Runs that exist but are not yet visible to any API read — the
        # unevaluated ref-update events. `settle` surfaces them,
        # modeling the passage of time.
        self.delayed_action_tasks: dict[str, list[dict]] = {}
        # Names whose Actions unit was explicitly disabled since creation
        # or the last enable — the window the seed push must happen in.
        self.disabled: set[str] = set()

    def settle(self) -> None:
        for name, tasks in self.delayed_action_tasks.items():
            self.action_tasks.setdefault(name, []).extend(tasks)
        self.delayed_action_tasks.clear()

    def ensure_org(self) -> None:
        (self.root / self.org).mkdir(parents=True, exist_ok=True)

    def clone_url(self, name: str) -> str:
        return f"file://{self._bare(name)}"

    def repository(self, name: str) -> dict | None:
        bare = self._bare(name)
        if not bare.is_dir():
            return None
        result = subprocess.run(("git", "show-ref", "--heads", "--quiet"), cwd=bare)
        return {
            "empty": result.returncode != 0,
            "has_actions": self.actions.get(name, False),
            "private": self.visibility.get(name, "public") == "private",
        }

    def prepare_empty_repository(
        self,
        name: str,
        default_branch: str,
        visibility: RepositoryVisibility,
        *,
        replace: bool,
    ) -> None:
        current = self.repository(name)
        if current is not None and current["empty"] is False:
            if not replace:
                raise WorldError(f"refusing to replace hub/{name}")
            shutil.rmtree(self._bare(name))
        if not self._bare(name).exists():
            self._bare(name).parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                (
                    "git",
                    "init",
                    "--bare",
                    f"--initial-branch={default_branch}",
                    str(self._bare(name)),
                ),
                check=True,
                capture_output=True,
            )
            # Forgejo 15 creates repositories with Actions enabled
            # (verified live); the real client immediately disables.
            self.actions[name] = True
        self.visibility[name] = visibility
        self.disable_actions(name)

    def disable_actions(self, name: str) -> None:
        self.actions[name] = False
        self.disabled.add(name)

    def configure_repository(
        self, name: str, default_branch: str, visibility: RepositoryVisibility
    ) -> None:
        # Seeding no longer enables Actions; it only settles the branch.
        git(
            "symbolic-ref", "HEAD", f"refs/heads/{default_branch}", cwd=self._bare(name)
        )
        self.visibility[name] = visibility

    def enable_actions(self, name: str) -> None:
        # Models the real hazard: enabling Actions makes Forgejo evaluate the
        # refs it holds, so any workflow-bearing tag pushed while the unit was
        # never disabled queues an upstream run. Seeding keeps every push
        # inside a disabled window, so a correctly seeded world queues nothing
        # here -- which is exactly what the separate command exists to make
        # true.
        if name not in self.disabled:
            for tag, sha in self._tags(name).items():
                trees = git(
                    "ls-tree",
                    "-r",
                    "--name-only",
                    sha,
                    "--",
                    ".github/workflows",
                    cwd=self._bare(name),
                )
                if trees:
                    self.action_tasks.setdefault(name, []).append(
                        {"tag": tag, "workflow": trees.splitlines()[0]}
                    )
        self.disabled.discard(name)
        self.actions[name] = True

    def queued_action_tasks(self, name: str) -> int:
        return len(self.action_tasks.get(name, []))

    def active_action_runs(self, name: str) -> int:
        # The fake never completes a run, so the queue equals the history.
        return len(self.action_tasks.get(name, []))

    def wait_until_nonempty(self, name: str) -> None:
        if self.repository(name) is None or self.repository(name)["empty"]:
            raise WorldError(f"hub/{name} is empty")

    def assert_seed_recovery_safe(
        self, name: str, *, expected_tags: dict[str, str] | None = None
    ) -> None:
        state = self.repository(name)
        if state is None or state["empty"]:
            raise WorldError(f"hub/{name} is absent or empty")
        for tag, sha in self._tags(name).items():
            if (expected_tags or {}).get(tag) != sha:
                raise WorldError(f"hub/{name} contains unexpected tags")
        if self.action_tasks.get(name):
            raise WorldError(f"hub/{name} has Actions history")

    def assert_repository_state(
        self,
        name: str,
        *,
        expected_tags: dict[str, str] | None = None,
        require_no_action_tasks: bool = False,
        require_actions: bool | None = True,
        expected_visibility: RepositoryVisibility | None = None,
    ) -> None:
        state = self.repository(name)
        if state is None or state["empty"]:
            raise WorldError(f"hub/{name} is absent or empty")
        if expected_visibility is not None and state["private"] != (
            expected_visibility == "private"
        ):
            raise WorldError(f"hub/{name} has wrong visibility")
        if require_actions is True and not state["has_actions"]:
            raise WorldError(f"hub/{name} does not have Actions enabled")
        if require_actions is False and state["has_actions"]:
            raise WorldError(f"hub/{name} has Actions enabled during seeding")
        if expected_tags is not None and self._tags(name) != expected_tags:
            raise WorldError(f"hub/{name} tags do not match the seed")
        branch = git("symbolic-ref", "--short", "HEAD", cwd=self._bare(name))
        workflows = git(
            "ls-tree",
            "-r",
            "--name-only",
            branch,
            "--",
            ".github/workflows",
            cwd=self._bare(name),
        )
        if workflows:
            raise WorldError(f"hub/{name} contains upstream workflows")
        if require_no_action_tasks and self.action_tasks.get(name):
            raise WorldError(f"hub/{name} queued Actions")

    def _tags(self, name: str) -> dict[str, str]:
        # Peeled commit first for annotated tags; lightweight tags fall
        # back to the object itself. lstrip=2 gives raw tag names even
        # when a branch shares the name.
        layout = "%(refname:lstrip=2) %(*objectname)%(objectname)"
        listing = subprocess.run(
            ("git", "for-each-ref", f"--format={layout}", "refs/tags"),
            cwd=self._bare(name),
            capture_output=True,
            text=True,
        ).stdout
        tags = {}
        for line in listing.splitlines():
            tag, _, sha = line.partition(" ")
            tags[tag] = sha[:40]
        return tags

    def _bare(self, name: str) -> Path:
        return self.root / self.org / f"{name}.git"


class FailingForge(FakeForge):
    def __init__(self, root: Path, fail_name: str) -> None:
        super().__init__(root)
        self.fail_name = fail_name

    def prepare_empty_repository(
        self,
        name: str,
        default_branch: str,
        visibility: RepositoryVisibility,
        *,
        replace: bool,
    ) -> None:
        if name == self.fail_name:
            raise WorldError("injected second-repository failure")
        super().prepare_empty_repository(
            name, default_branch, visibility, replace=replace
        )


class DelayedEmptyForge(FakeForge):
    """Model Forgejo accepting a push before clearing its API empty flag."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.wait_calls = 0

    def wait_until_nonempty(self, name: str) -> None:
        self.wait_calls += 1
        if self.wait_calls == 1:
            raise WorldError(f"hub/{name} is still marked empty")
        super().wait_until_nonempty(name)


class ReadOnlyForge(FakeForge):
    """Fail the test if adoption calls any mutating Forge operation."""

    def ensure_org(self) -> None:
        raise AssertionError("adoption must not create an organization")

    def prepare_empty_repository(
        self,
        name: str,
        default_branch: str,
        visibility: RepositoryVisibility,
        *,
        replace: bool,
    ) -> None:
        raise AssertionError("adoption must not prepare a repository")

    def configure_repository(
        self, name: str, default_branch: str, visibility: RepositoryVisibility
    ) -> None:
        raise AssertionError("adoption must not configure a repository")


class EngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    def engine(
        self,
        manifest_path: Path,
        forge: FakeForge,
        *,
        lock_name: str = "world.lock.json",
        now: datetime = NOW,
        sleep: Callable[[float], None] | None = None,
    ) -> WorldEngine:
        return WorldEngine(
            document=load_manifest(manifest_path),
            lock_path=self.root / lock_name,
            forge=forge,
            cache_dir=self.root / "cache",
            hub_commit=SWECRAFT_COMMIT,
            now=lambda: now,
            sleep=sleep or (lambda _seconds: None),
        )

    def test_seed_leaves_actions_disabled_and_queues_nothing(self) -> None:
        """The seed must not enable Actions.

        `git push` returns once refs are accepted, but Forgejo evaluates the
        ref-update events asynchronously. Enabling inside the seed raced that
        evaluation and fired every workflow carried in every pushed tag tree;
        a 26-repository world produced 18,331 queued jobs that way.
        """

        upstream, _ = seed_upstream(self.root, "demo")
        manifest = write_manifest(self.root, [entry("demo", upstream)])
        forge = FakeForge(self.root)

        lock = self.engine(manifest, forge).seed()

        self.assertFalse(forge.actions["demo"])
        self.assertFalse(lock.repos[0].actions_enabled)
        self.assertEqual(forge.queued_action_tasks("demo"), 0)

    def test_private_visibility_is_seeded_locked_and_verified(self) -> None:
        upstream, _ = seed_upstream(self.root, "demo")
        manifest = write_manifest(
            self.root, [entry("demo", upstream, visibility="private")]
        )
        forge = FakeForge(self.root)
        engine = self.engine(manifest, forge)

        lock = engine.seed()

        self.assertEqual(lock.repos[0].visibility, "private")
        self.assertTrue(forge.repository("demo")["private"])
        self.assertEqual(engine.check(), lock)
        forge.visibility["demo"] = "public"
        with self.assertRaisesRegex(WorldError, "visibility"):
            engine.check()

    def test_enable_actions_turns_the_world_on_and_relocks(self) -> None:
        upstream, _ = seed_upstream(self.root, "demo")
        manifest = write_manifest(self.root, [entry("demo", upstream)])
        forge = FakeForge(self.root)
        engine = self.engine(manifest, forge)
        engine.seed()

        lock = engine.enable_actions()

        self.assertTrue(forge.actions["demo"])
        self.assertTrue(lock.repos[0].actions_enabled)
        # The lock is an observation, so the new state must be on disk too.
        self.assertTrue(load_lock(engine.lock_path).repos[0].actions_enabled)
        # Enabling after the pushes were evaluated queues nothing.
        self.assertEqual(forge.queued_action_tasks("demo"), 0)

    def test_enable_actions_is_idempotent(self) -> None:
        upstream, _ = seed_upstream(self.root, "demo")
        manifest = write_manifest(self.root, [entry("demo", upstream)])
        forge = FakeForge(self.root)
        engine = self.engine(manifest, forge)
        engine.seed()
        engine.enable_actions()

        lock = engine.enable_actions()

        self.assertTrue(lock.repos[0].actions_enabled)
        self.assertEqual(forge.queued_action_tasks("demo"), 0)

    def test_enable_actions_refuses_a_contaminated_world(self) -> None:
        """A world that already queued runs must fail here, not mid-campaign."""

        upstream, _ = seed_upstream(self.root, "demo")
        manifest = write_manifest(self.root, [entry("demo", upstream)])
        forge = FakeForge(self.root)
        engine = self.engine(manifest, forge)
        engine.seed()
        forge.action_tasks["demo"] = [{"tag": "v1", "workflow": "ci.yml"}]

        with self.assertRaises(WorldError) as caught:
            engine.enable_actions()

        self.assertIn("contaminated", str(caught.exception))
        self.assertFalse(forge.actions["demo"])
        self.assertFalse(load_lock(engine.lock_path).repos[0].actions_enabled)

    def test_enable_actions_detects_runs_that_surface_after_the_switch(self) -> None:
        """Contamination that appears only after enabling must still fail.

        Unevaluated ref-update events are invisible to every API read, so the
        pre-enable check can pass while runs surface seconds later — a
        reproduction showed 0 runs at the instant of enabling and 100 within
        5 seconds. The post-enable settle window must catch them, disable
        Actions again, and leave the lock recording `actions_enabled: false`.
        """

        upstream, _ = seed_upstream(self.root, "demo")
        manifest = write_manifest(self.root, [entry("demo", upstream)])
        forge = FakeForge(self.root)
        sleeps: list[float] = []

        def settling_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            if len(sleeps) == 2:
                forge.settle()

        engine = self.engine(manifest, forge, sleep=settling_sleep)
        engine.seed()
        forge.delayed_action_tasks["demo"] = [{"tag": "v1", "workflow": "ci.yml"}]

        with self.assertRaises(WorldError) as caught:
            engine.enable_actions()

        self.assertIn("surfaced", str(caught.exception))
        self.assertEqual(sleeps, [ACTIONS_SETTLE_SECONDS, ACTIONS_SETTLE_SECONDS])
        self.assertFalse(forge.actions["demo"])
        self.assertFalse(load_lock(engine.lock_path).repos[0].actions_enabled)

    def test_enable_actions_requires_a_complete_seed_lock(self) -> None:
        upstream, _ = seed_upstream(self.root, "demo")
        manifest = write_manifest(self.root, [entry("demo", upstream)])
        engine = self.engine(manifest, FakeForge(self.root))

        with self.assertRaises(WorldError) as caught:
            engine.enable_actions()

        self.assertIn("complete seed lock", str(caught.exception))

    def test_seed_writes_verified_baseline_and_exactly_the_pin_tag(self) -> None:
        upstream, pin = seed_upstream(self.root, "demo")
        manifest = write_manifest(self.root, [entry("demo", upstream)])
        forge = FakeForge(self.root)
        engine = self.engine(manifest, forge)

        lock = engine.seed()

        self.assertEqual(lock.status, "complete")
        record = lock.repos[0]
        self.assertEqual(record.pin_commit, pin)
        self.assertNotEqual(record.baseline_commit, pin)
        self.assertEqual(record.stripped_paths, (".github/workflows/ci.yml",))
        bare = forge._bare("demo")
        self.assertEqual(git("rev-parse", f"{record.baseline_commit}^", cwd=bare), pin)
        self.assertEqual(
            git(
                "diff",
                "--name-status",
                "--no-renames",
                record.pin_commit,
                record.baseline_commit,
                cwd=bare,
            ),
            "A\t.github/workflows-upstream/README.md\n"
            "A\t.github/workflows-upstream/ci.yml\n"
            "D\t.github/workflows/ci.yml",
        )
        self.assertEqual(
            git(
                "show",
                f"{record.baseline_commit}:.github/workflows-upstream/ci.yml",
                cwd=bare,
            ),
            git("show", f"{record.pin_commit}:.github/workflows/ci.yml", cwd=bare),
        )
        # The pin tag is seeded — the true upstream name at the true
        # upstream commit — and nothing else.
        self.assertEqual(record.seed_tag, "v1")
        self.assertEqual(git("rev-parse", "v1^{commit}", cwd=bare), pin)
        self.assertEqual(forge._tags("demo"), {"v1": pin})
        self.assertEqual(engine.check(), lock)
        # Agents adding release tags later never break verification.
        git("tag", "agent-release-v1", record.baseline_commit, cwd=bare)
        self.assertEqual(engine.check(), lock)

    def test_seed_excludes_the_pin_s_future_but_keeps_side_branches(self) -> None:
        """A world must not ship the answer to its own experiment.

        Pinning urllib3 at 2.7.0 while the 2.8.0 tag rode along — 69
        commits ahead, already carrying the URL optimization its
        maintainer then rediscovered — is what made one of our runs
        unwinnable. Later releases of the pinned line must not be seeded.
        Tags that merely exist elsewhere in history must be, because a
        maintenance branch is history a maintainer is entitled to read.
        """

        upstream, root_commit = seed_upstream(self.root, "demo")
        # seed_upstream tags v1 on the root commit; move the pin forward so
        # the fixture has a real "before the pin" to branch from.
        git("tag", "-d", "v1", cwd=upstream)
        # A maintenance release off the root, descending from nothing the
        # pin can reach: urllib3's 1.26.x shape.
        git("checkout", "-b", "maintenance", root_commit, cwd=upstream)
        (upstream / "BACKPORT.md").write_text("backport\n", encoding="utf-8")
        git("add", ".", cwd=upstream)
        git("commit", "-m", "maintenance backport", cwd=upstream)
        git("tag", "-a", "v0.9-maintenance", "-m", "old line", cwd=upstream)
        maintenance = git("rev-parse", "HEAD^{commit}", cwd=upstream)
        # The pin itself, one commit past the root.
        git("checkout", "main", cwd=upstream)
        (upstream / "PINNED.md").write_text("pinned state\n", encoding="utf-8")
        git("add", ".", cwd=upstream)
        git("commit", "-m", "the pinned release", cwd=upstream)
        git("tag", "-a", "v1", "-m", "version one", cwd=upstream)
        pin = git("rev-parse", "HEAD^{commit}", cwd=upstream)
        # ...and then its future, carrying the answer.
        (upstream / "OPTIMIZATION.md").write_text("the answer\n", encoding="utf-8")
        git("add", ".", cwd=upstream)
        git("commit", "-m", "the optimization the agent should discover", cwd=upstream)
        git("tag", "-a", "v2", "-m", "version two", cwd=upstream)

        manifest = write_manifest(self.root, [entry("demo", upstream)])
        forge = FakeForge(self.root)
        engine = self.engine(manifest, forge)

        lock = engine.seed()

        self.assertEqual(lock.status, "complete")
        seeded = forge._tags("demo")
        self.assertEqual(seeded, {"v1": pin, "v0.9-maintenance": maintenance})
        self.assertNotIn("v2", seeded)
        # The future is absent from the forge entirely, not merely
        # untagged: resolving it must fail.
        bare = forge._bare("demo")
        with self.assertRaises(subprocess.CalledProcessError):
            git("rev-parse", "--verify", "v2^{commit}", cwd=bare)
        self.assertEqual(engine.check(), lock)

    def test_recovery_does_not_reintroduce_a_future_tag(self) -> None:
        """The recovery push repairs the seed's tags, never the pin's
        future — `push_all_tags` must honour the same exclusion."""

        upstream, pin = seed_upstream(self.root, "demo")
        git("checkout", "-b", "later", cwd=upstream)
        (upstream / "OPTIMIZATION.md").write_text("the answer\n", encoding="utf-8")
        git("add", ".", cwd=upstream)
        git("commit", "-m", "later release", cwd=upstream)
        git("tag", "-a", "v2", "-m", "version two", cwd=upstream)
        git("checkout", "main", cwd=upstream)

        manifest = write_manifest(self.root, [entry("demo", upstream)])
        forge = FakeForge(self.root)
        self.engine(manifest, forge).seed()

        # Strand the seed the way an interrupted push does, then recover.
        bare = forge._bare("demo")
        git("update-ref", "-d", "refs/tags/v1", cwd=bare)
        (manifest.parent / "world.lock.json").unlink()
        self.assertEqual(forge._tags("demo"), {})

        lock = self.engine(manifest, forge).seed()

        self.assertEqual(lock.status, "complete")
        self.assertEqual(forge._tags("demo"), {"v1": pin})

    def test_recovery_repairs_a_missing_pin_tag(self) -> None:
        """A stranded seed (branch landed, tag absent) must complete on
        rerun: the tag is repaired inside a re-disabled window, and the
        hazard guard proves the window was honoured."""

        upstream, pin = seed_upstream(self.root, "demo")
        manifest = write_manifest(self.root, [entry("demo", upstream)])
        forge = FakeForge(self.root)
        engine = self.engine(manifest, forge)
        engine.seed()
        bare = forge._bare("demo")
        git("update-ref", "-d", "refs/tags/v1", cwd=bare)
        (manifest.parent / "world.lock.json").unlink()

        lock = self.engine(manifest, forge).seed()

        self.assertEqual(lock.status, "complete")
        self.assertEqual(forge._tags("demo"), {"v1": pin})
        self.assertEqual(forge.action_tasks.get("demo", []), [])

    def test_commit_pins_still_seed_the_upstream_tags(self) -> None:
        """Tag preservation is history fidelity, independent of how the
        world pins: a commit-pinned repo carries its upstream tags too;
        only the seed_tag anchor stays unset."""

        upstream, pin = seed_upstream(self.root, "demo")
        manifest = write_manifest(
            self.root,
            [entry("demo", upstream, pin={"type": "commit", "value": pin})],
        )
        forge = FakeForge(self.root)
        engine = self.engine(manifest, forge)
        lock = engine.seed()
        self.assertIsNone(lock.repos[0].seed_tag)
        self.assertEqual(forge._tags("demo"), {"v1": pin})

    def test_seed_preserves_upstream_tag_history_up_to_the_pin(self) -> None:
        """Everything the pin can already see is kept — historical
        releases and side branches alike. Only its future is withheld,
        which `test_seed_excludes_the_pin_s_future_but_keeps_side_branches`
        covers."""

        upstream, pin = seed_upstream(self.root, "demo")
        first = git("rev-list", "--max-parents=0", "HEAD", cwd=upstream)
        git("tag", "-a", "v0", first, "-m", "historical release", cwd=upstream)
        # A branch sharing a tag's name must not smuggle "tags/v1" into
        # the expectation (refname:short would).
        git("branch", "v1", pin, cwd=upstream)
        manifest = write_manifest(self.root, [entry("demo", upstream)])
        forge = FakeForge(self.root)
        engine = self.engine(manifest, forge)
        lock = engine.seed()
        self.assertEqual(lock.repos[0].seed_tag, "v1")
        self.assertEqual(forge._tags("demo"), {"v0": first, "v1": pin})
        self.assertEqual(engine.check(), lock)

    def test_upstream_workflows_readme_survives_relocation(self) -> None:
        # An upstream may document its CI at .github/workflows/README.md;
        # relocation must keep that file rather than overwrite it with the
        # seeder's explainer, and the transform proof must expect exactly
        # that shape.
        upstream, _ = seed_upstream(self.root, "demo", workflows_readme=True)
        manifest = write_manifest(self.root, [entry("demo", upstream)])
        forge = FakeForge(self.root)
        engine = self.engine(manifest, forge)
        lock = engine.seed()
        record = lock.repos[0]
        self.assertEqual(
            record.stripped_paths,
            (".github/workflows/README.md", ".github/workflows/ci.yml"),
        )
        bare = forge._bare("demo")
        relocated = git(
            "show",
            f"{record.baseline_commit}:.github/workflows-upstream/README.md",
            cwd=bare,
        )
        self.assertEqual(relocated, "# Upstream's own CI docs")
        self.assertEqual(engine.check(), lock)

    def test_strip_protocol_still_deletes_workflows(self) -> None:
        # The pre-relocate transform stays reproducible for old worlds.
        upstream, pin = seed_upstream(self.root, "demo")
        manifest = write_manifest(
            self.root,
            [{**entry("demo", upstream), "seed_protocol": "strip-github-actions-v1"}],
        )
        forge = FakeForge(self.root)
        engine = self.engine(manifest, forge)
        lock = engine.seed()
        record = lock.repos[0]
        bare = forge._bare("demo")
        self.assertEqual(
            git(
                "diff",
                "--name-status",
                "--no-renames",
                record.pin_commit,
                record.baseline_commit,
                cwd=bare,
            ),
            "D\t.github/workflows/ci.yml",
        )
        self.assertEqual(engine.check(), lock)

    def test_seed_repushes_when_forgejo_delays_clearing_empty(self) -> None:
        upstream, _ = seed_upstream(self.root, "demo")
        manifest = write_manifest(self.root, [entry("demo", upstream)])
        forge = DelayedEmptyForge(self.root)
        reports: list[str] = []
        engine = WorldEngine(
            document=load_manifest(manifest),
            lock_path=self.root / "world.lock.json",
            forge=forge,
            cache_dir=self.root / "cache",
            hub_commit=SWECRAFT_COMMIT,
            now=lambda: NOW,
            report=reports.append,
        )
        original_push = engine_module.push_seed

        with patch.object(engine_module, "push_seed", wraps=original_push) as pushed:
            lock = engine.seed()

        self.assertEqual(lock.status, "complete")
        self.assertEqual(forge.wait_calls, 2)
        self.assertEqual(pushed.call_count, 2)
        self.assertTrue(any("retrying ref" in message for message in reports))

    def test_full_commit_pin_seeds_without_an_ambiguous_ref(self) -> None:
        upstream, pin = seed_upstream(self.root, "committed", workflows=False)
        repository = entry("committed", upstream)
        repository["pin"] = {"type": "commit", "value": pin}
        manifest = write_manifest(self.root, [repository])

        lock = self.engine(manifest, FakeForge(self.root)).seed()

        self.assertEqual(lock.repos[0].pin_commit, pin)
        self.assertEqual(lock.repos[0].baseline_commit, pin)
        self.assertEqual(lock.repos[0].pin_ref, pin)

    def test_complete_seed_rerun_is_a_byte_for_byte_noop(self) -> None:
        upstream, _ = seed_upstream(self.root, "demo")
        manifest = write_manifest(self.root, [entry("demo", upstream)])
        forge = FakeForge(self.root)
        engine = self.engine(manifest, forge)
        engine.seed()
        lock_path = self.root / "world.lock.json"
        before = lock_path.read_bytes()

        later = self.engine(
            manifest,
            forge,
            now=datetime(2030, 1, 1, tzinfo=UTC),
        ).seed()

        self.assertEqual(lock_path.read_bytes(), before)
        self.assertEqual(later.seeded_at, NOW)

    def test_partial_seed_resumes_without_reseeding_verified_prefix(self) -> None:
        one, _ = seed_upstream(self.root, "one")
        two, _ = seed_upstream(self.root, "two")
        manifest = write_manifest(self.root, [entry("one", one), entry("two", two)])
        failing = FailingForge(self.root, "two")

        with self.assertRaisesRegex(WorldError, "injected"):
            self.engine(manifest, failing).seed()

        partial = load_lock(self.root / "world.lock.json")
        self.assertEqual(partial.status, "partial")
        self.assertEqual([record.name for record in partial.repos], ["one"])

        resumed_forge = FakeForge(self.root)
        resumed_forge.actions = failing.actions
        complete = self.engine(
            manifest,
            resumed_forge,
            now=datetime(2030, 1, 1, tzinfo=UTC),
        ).seed()
        self.assertEqual(complete.status, "complete")
        self.assertEqual([record.name for record in complete.repos], ["one", "two"])
        self.assertEqual(complete.seeded_at, NOW)

    def test_seed_adopts_an_exact_live_seed_when_the_lock_was_lost(self) -> None:
        upstream, _ = seed_upstream(self.root, "demo")
        manifest = write_manifest(self.root, [entry("demo", upstream)])
        forge = FakeForge(self.root)
        original = self.engine(manifest, forge).seed()
        (self.root / "world.lock.json").unlink()
        # Model a crash after the ref push but before repository configuration:
        # exact seed recovery may repair Actions only after proving Git safety.
        forge.actions["demo"] = False

        recovered = self.engine(manifest, forge, lock_name="recovered.lock.json").seed()

        self.assertEqual(
            recovered.repos[0].baseline_commit,
            original.repos[0].baseline_commit,
        )
        # Recovery reconfigures the repository but must not enable Actions:
        # that belongs to `enable-actions`, run once the world is seeded.
        self.assertFalse(forge.actions["demo"])

    def test_seed_refuses_unrelated_repository_without_configuring_it(self) -> None:
        upstream, _ = seed_upstream(self.root, "demo")
        manifest = write_manifest(self.root, [entry("demo", upstream)])
        forge = FakeForge(self.root)
        forge.ensure_org()
        forge.prepare_empty_repository("demo", "main", "public", replace=False)
        unrelated = self.root / "unrelated"
        unrelated.mkdir()
        git("init", "--initial-branch=main", cwd=unrelated)
        git("config", "user.name", "Other", cwd=unrelated)
        git("config", "user.email", "other@example.test", cwd=unrelated)
        (unrelated / "other.txt").write_text("unrelated\n", encoding="utf-8")
        git("add", ".", cwd=unrelated)
        git("commit", "-m", "unrelated history", cwd=unrelated)
        git("push", forge.clone_url("demo"), "main", cwd=unrelated)
        forge.configure_repository("demo", "main", "public")

        with patch.object(
            forge, "configure_repository", wraps=forge.configure_repository
        ) as configured:
            with self.assertRaisesRegex(WorldError, "pin .* not an ancestor"):
                self.engine(manifest, forge).seed()

        configured.assert_not_called()

    def test_seed_refuses_evolved_repository_without_configuring_it(self) -> None:
        upstream, _ = seed_upstream(self.root, "demo")
        manifest = write_manifest(self.root, [entry("demo", upstream)])
        forge = FakeForge(self.root)
        self.engine(manifest, forge).seed()
        (self.root / "world.lock.json").unlink()
        checkout = self.root / "evolved"
        git("clone", forge.clone_url("demo"), str(checkout), cwd=self.root)
        git("config", "user.name", "Agent", cwd=checkout)
        git("config", "user.email", "agent@example.test", cwd=checkout)
        (checkout / "agent.txt").write_text("evolved\n", encoding="utf-8")
        git("add", ".", cwd=checkout)
        git("commit", "-m", "agent change", cwd=checkout)
        git("push", "origin", "main", cwd=checkout)

        with patch.object(
            forge, "configure_repository", wraps=forge.configure_repository
        ) as configured:
            with self.assertRaisesRegex(WorldError, "evolved beyond"):
                self.engine(manifest, forge, lock_name="recovered.lock.json").seed()

        configured.assert_not_called()

    def test_adopt_records_seed_baseline_separately_from_evolved_head(self) -> None:
        upstream, _ = seed_upstream(self.root, "demo")
        manifest = write_manifest(self.root, [entry("demo", upstream)])
        forge = FakeForge(self.root)
        seeded = self.engine(manifest, forge).seed()
        (self.root / "world.lock.json").unlink()
        checkout = self.root / "agent"
        git("clone", forge.clone_url("demo"), str(checkout), cwd=self.root)
        git("config", "user.name", "Agent", cwd=checkout)
        git("config", "user.email", "agent@example.test", cwd=checkout)
        (checkout / "agent.txt").write_text("evolved\n", encoding="utf-8")
        git("add", ".", cwd=checkout)
        git("commit", "-m", "agent change", cwd=checkout)
        git("push", "origin", "main", cwd=checkout)
        evolved = git("rev-parse", "HEAD", cwd=checkout)

        read_only_forge = ReadOnlyForge(self.root)
        read_only_forge.actions = forge.actions
        adopted = self.engine(
            manifest, read_only_forge, lock_name="adopted.lock.json"
        ).adopt()

        record = adopted.repos[0]
        self.assertEqual(record.baseline_commit, seeded.repos[0].baseline_commit)
        self.assertEqual(record.current_head_at_adoption, evolved)
        self.assertIsNone(adopted.seeded_at)
        self.assertEqual(adopted.adopted_at, NOW)

    def test_check_rejects_a_live_branch_that_lost_its_baseline(self) -> None:
        upstream, pin = seed_upstream(self.root, "demo")
        manifest = write_manifest(self.root, [entry("demo", upstream)])
        forge = FakeForge(self.root)
        engine = self.engine(manifest, forge)
        engine.seed()
        git("update-ref", "refs/heads/main", pin, cwd=forge._bare("demo"))

        with self.assertRaisesRegex(WorldError, "upstream workflows|not an ancestor"):
            engine.check()

    def test_declared_duplicate_must_resolve_to_the_primary_commit(self) -> None:
        one, _ = seed_upstream(self.root, "one")
        two, _ = seed_upstream(self.root, "two")
        manifest = write_manifest(
            self.root,
            [
                entry("one", one),
                entry("two", two, duplicate_pin_of="one"),
            ],
        )

        with self.assertRaisesRegex(WorldError, "different commit"):
            self.engine(manifest, FakeForge(self.root)).seed()

    def test_example_manifest_is_valid(self) -> None:
        example = Path(__file__).resolve().parents[3] / "examples" / "world.yaml"
        document = load_manifest(example)
        self.assertEqual(document.manifest.world_id, "experiment-2")
        self.assertEqual(len(document.manifest.repos), 4)


if __name__ == "__main__":
    unittest.main()
