"""Manifest-driven seed, adopt, and live-world verification orchestration."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from swecraft_world.errors import WorldError
from swecraft_world.gitops import (
    ResolvedRepository,
    discover_adoption_baseline,
    fetch_live,
    lock_record,
    prepare_seed,
    prepared_as_lock,
    push_all_tags,
    push_seed,
    resolve_repositories,
    seed_tags,
    verify_live_record,
)
from swecraft_world.io import (
    ManifestDocument,
    load_lock,
    validate_lock,
    write_lock_atomic,
)
from swecraft_world.lifecycle import world_mutation_lock
from swecraft_world.models import RepositoryVisibility, WorldLock, WorldLockRepository


class ForgeBackend(Protocol):
    """The Forge operations the engine needs; fakeable without Docker."""

    base_url: str
    token: str
    org: str

    def ensure_org(self) -> None: ...

    def clone_url(self, name: str) -> str: ...

    def repository(self, name: str) -> dict[str, Any] | None: ...

    def prepare_empty_repository(
        self,
        name: str,
        default_branch: str,
        visibility: RepositoryVisibility,
        *,
        replace: bool,
    ) -> None: ...

    def configure_repository(
        self, name: str, default_branch: str, visibility: RepositoryVisibility
    ) -> None: ...

    def disable_actions(self, name: str) -> None: ...

    def wait_until_nonempty(self, name: str) -> None: ...

    def assert_seed_recovery_safe(
        self, name: str, *, expected_tags: dict[str, str] | None = None
    ) -> None: ...

    def assert_repository_state(
        self,
        name: str,
        *,
        expected_tags: dict[str, str] | None = None,
        require_no_action_tasks: bool = False,
        require_actions: bool | None = True,
        expected_visibility: RepositoryVisibility | None = None,
    ) -> None: ...

    def enable_actions(self, name: str) -> None: ...

    def queued_action_tasks(self, name: str) -> int: ...

    def active_action_runs(self, name: str) -> int: ...


def _seed_tags(resolved) -> dict[str, str]:
    """Exactly the tags the seed pushes — the pin's future excluded."""

    return seed_tags(resolved)


# Forgejo evaluates ref-update events asynchronously, and unevaluated events
# are invisible to every API read. Reproduced on 15.0.4: after pushing 50
# workflow-bearing tags, a repository enabled immediately showed 0 runs at
# that instant and 100 runs 5 seconds later, while a 5-second pause before
# enabling produced 0 runs. Both settle windows in `enable_actions` use this
# value, with margin over the measured 5 seconds.
ACTIONS_SETTLE_SECONDS = 10.0


class WorldEngine:
    """Execute the world protocol while keeping its lock authoritative."""

    def __init__(
        self,
        *,
        document: ManifestDocument,
        lock_path: Path,
        forge: ForgeBackend,
        cache_dir: Path,
        hub_commit: str,
        now: Callable[[], datetime] | None = None,
        report: Callable[[str], None] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.document = document
        self.lock_path = lock_path
        self.forge = forge
        self.cache_dir = cache_dir
        self.hub_commit = hub_commit
        self.now = now or (lambda: datetime.now(UTC))
        self.report = report or (lambda _message: None)
        self.sleep = sleep or time.sleep
        if forge.org != document.manifest.org:
            raise WorldError(
                f"Forge organization is {forge.org!r}, but the manifest uses "
                f"{document.manifest.org!r}"
            )

    def seed(self, *, replace: bool = False) -> WorldLock:
        """Create missing seed baselines, journaling after each verification."""

        with world_mutation_lock(self.lock_path):
            return self._seed(replace=replace)

    def _seed(self, *, replace: bool) -> WorldLock:
        resolved = self._resolve()
        existing = self._existing("seed")
        if existing is not None and existing.status == "complete":
            self._verify_all(resolved, existing)
            self.report("world already matches its complete lock")
            return existing

        self.forge.ensure_org()
        records = list(existing.repos if existing else ())
        self._verify_prefix(resolved, records)
        for repository in resolved[len(records) :]:
            record = self._seed_one(repository, replace=replace)
            records.append(record)
            lock = self._lock(records, mode="seed", existing=existing)
            write_lock_atomic(self.lock_path, lock)
            self.report(
                f"seeded {repository.intent.name} at {record.baseline_commit[:12]}"
            )
            existing = lock
        assert existing is not None
        return existing

    def adopt(self) -> WorldLock:
        """Record a pre-existing world without moving any live repository."""

        with world_mutation_lock(self.lock_path):
            return self._adopt()

    def _adopt(self) -> WorldLock:
        resolved = self._resolve()
        existing = self._existing("adopt")
        if existing is not None and existing.status == "complete":
            self._verify_all(resolved, existing)
            self.report("world already matches its complete adopted lock")
            return existing

        records = list(existing.repos if existing else ())
        self._verify_prefix(resolved, records)
        for repository in resolved[len(records) :]:
            # Adoption observes an existing world; it does not dictate
            # whether Actions is on, and records what it finds.
            self.forge.assert_repository_state(
                repository.intent.name,
                require_actions=None,
                expected_visibility=repository.intent.visibility,
            )
            live_state = self.forge.repository(repository.intent.name) or {}
            with fetch_live(
                repository,
                self.forge.clone_url(repository.intent.name),
                self.forge.token,
            ) as live:
                baseline, baseline_tree = discover_adoption_baseline(live, repository)
                record = lock_record(
                    repository,
                    baseline_commit=baseline,
                    baseline_tree=baseline_tree,
                    actions_enabled=live_state.get("has_actions") is True,
                    current_head=live.current_head,
                    adopted=True,
                )
            records.append(record)
            lock = self._lock(records, mode="adopt", existing=existing)
            write_lock_atomic(self.lock_path, lock)
            self.report(
                f"adopted {repository.intent.name} with baseline "
                f"{record.baseline_commit[:12]}"
            )
            existing = lock
        assert existing is not None
        return existing

    def check(self) -> WorldLock:
        """Prove a complete lock against upstream resolution and live Forgejo."""

        resolved = self._resolve()
        if not self.lock_path.is_file():
            raise WorldError(f"world lock not found: {self.lock_path}")
        lock = load_lock(self.lock_path)
        validate_lock(self.document, lock)
        self._verify_all(resolved, lock)
        self.report(f"verified {len(lock.repos)} world repositories")
        return lock

    def _resolve(self) -> tuple[ResolvedRepository, ...]:
        resolved = resolve_repositories(self.document.manifest, self.cache_dir)
        self.report(f"resolved {len(resolved)} upstream pins")
        return resolved

    def _existing(self, mode: str) -> WorldLock | None:
        if not self.lock_path.exists():
            return None
        lock = load_lock(self.lock_path)
        validate_lock(self.document, lock, require_complete=False)
        if mode == "seed" and lock.seeded_at is None:
            raise WorldError("an adopted world lock cannot be continued with seed")
        if mode == "adopt" and lock.adopted_at is None:
            raise WorldError("a seeded world lock cannot be continued with adopt")
        return lock

    def enable_actions(self) -> WorldLock:
        """Turn Forgejo Actions on across a seeded world, then re-lock.

        Deliberately separate from seeding. `git push` returns once refs are
        accepted, but Forgejo evaluates the ref-update events asynchronously;
        enabling Actions inside the seed raced that evaluation, so every
        workflow carried in every pushed tag tree fired against a repository
        that had just become enabled. A 26-repository world produced 18,331
        queued jobs that way, and the seed-time no-tasks assertion could not
        see them because the queue is legitimately empty at that instant.

        The command gap alone is procedural, so the safety contract also
        lives here in code. Unevaluated ref-update events are invisible to
        every API read, so the pre-enable run check proves nothing about a
        push made moments ago; only elapsed time covers it, hence the settle
        delay before enabling. After enabling, contamination becomes visible
        within seconds (measured: 0 runs at the instant of enabling, 100
        within 5 seconds), so a second settle window and re-check catch it
        while Actions can still be disabled again and before the lock records
        `actions_enabled: true`.
        """

        with world_mutation_lock(self.lock_path):
            lock = self._existing("seed")
            if lock is None or lock.status != "complete":
                raise WorldError(
                    "enable-actions needs a complete seed lock; seed the world first"
                )
            pending: list[WorldLockRepository] = []
            for record in lock.repos:
                if record.actions_enabled:
                    self.report(f"{record.name}: Actions already enabled")
                    continue
                queued = self.forge.queued_action_tasks(record.name)
                if queued:
                    raise WorldError(
                        f"{record.name} already has {queued} Actions run(s) "
                        "before Actions was enabled; the world is contaminated "
                        "and must be re-seeded"
                    )
                pending.append(record)
            if not pending:
                return lock
            self.sleep(ACTIONS_SETTLE_SECONDS)
            for record in pending:
                self.forge.enable_actions(record.name)
                self.forge.assert_repository_state(
                    record.name,
                    require_actions=True,
                    expected_visibility=record.visibility,
                )
                self.report(f"enabled Actions on {record.name}")
            self.sleep(ACTIONS_SETTLE_SECONDS)
            dirty = {
                record.name: queued
                for record in pending
                if (queued := self.forge.queued_action_tasks(record.name))
            }
            if dirty:
                for name in dirty:
                    self.forge.disable_actions(name)
                raise WorldError(
                    "enabling Actions surfaced queued runs: "
                    + ", ".join(f"{name}={n}" for name, n in sorted(dirty.items()))
                    + "; Actions was disabled again and the lock was not "
                    "rewritten; the world is contaminated and must be re-seeded"
                )
            records = tuple(
                record.model_copy(update={"actions_enabled": True})
                for record in lock.repos
            )
            updated = lock.model_copy(update={"repos": records})
            write_lock_atomic(self.lock_path, updated)
            return updated

    def settle_actions(self, *, expected_zero: bool = True) -> dict[str, int]:
        """Report waiting/running/blocked Actions runs per repository.

        Separate from `enable_actions` so an operator can check a world at any
        time -- before enabling, to prove the seed was clean, or afterwards,
        to see what the society has since queued against the runner. Counts
        only runs that are waiting for or occupying the runner; the
        contamination gate in `enable_actions` counts completed runs too,
        because a correct seed leaves no run history at all.
        """

        lock = self._existing("seed")
        if lock is None:
            raise WorldError("no seed lock to inspect")
        counts = {
            record.name: self.forge.active_action_runs(record.name)
            for record in lock.repos
        }
        if expected_zero:
            dirty = {name: n for name, n in counts.items() if n}
            if dirty:
                raise WorldError(
                    "repositories already carry Actions runs: "
                    + ", ".join(f"{name}={n}" for name, n in sorted(dirty.items()))
                )
        return counts

    def _verify_prefix(
        self,
        resolved: tuple[ResolvedRepository, ...],
        records: list[WorldLockRepository],
    ) -> None:
        for repository, record in zip(resolved, records, strict=False):
            self._verify_resolved_record(repository, record)
            self._verify_one(repository, record)
            self.report(f"verified existing lock entry {record.name}")

    def _verify_all(
        self, resolved: tuple[ResolvedRepository, ...], lock: WorldLock
    ) -> None:
        if len(resolved) != len(lock.repos):
            raise WorldError("resolved world and lock have different repository counts")
        for repository, record in zip(resolved, lock.repos, strict=True):
            self._verify_resolved_record(repository, record)
            self._verify_one(repository, record)

    def _verify_resolved_record(
        self, resolved: ResolvedRepository, record: WorldLockRepository
    ) -> None:
        if (
            resolved.pin_commit != record.pin_commit
            or resolved.pin_tree != record.pin_tree
            or resolved.stripped_paths != record.stripped_paths
        ):
            raise WorldError(
                f"{resolved.intent.name}: upstream resolution no longer matches "
                "the locked pin and seed transform"
            )

    def _verify_one(
        self, resolved: ResolvedRepository, record: WorldLockRepository
    ) -> None:
        self.forge.assert_repository_state(
            record.name,
            require_actions=record.actions_enabled,
            expected_visibility=record.visibility,
        )
        with fetch_live(
            resolved, self.forge.clone_url(record.name), self.forge.token
        ) as live:
            verify_live_record(live, record)

    def _seed_one(
        self, resolved: ResolvedRepository, *, replace: bool
    ) -> WorldLockRepository:
        current = self.forge.repository(resolved.intent.name)
        if current is not None and current.get("empty") is False and not replace:
            # Prove the Git state first. An evolved repository must be refused
            # without even repairing its Actions/default-branch configuration.
            with fetch_live(
                resolved,
                self.forge.clone_url(resolved.intent.name),
                self.forge.token,
            ) as live:
                baseline, baseline_tree = discover_adoption_baseline(live, resolved)
                if live.current_head != baseline:
                    raise WorldError(
                        f"{resolved.intent.name}: live repository has evolved beyond "
                        "its seed baseline; refusing to overwrite it"
                    )
                record = lock_record(
                    resolved,
                    baseline_commit=baseline,
                    baseline_tree=baseline_tree,
                )
            # A crash may have happened after the ref push but before Actions
            # was enabled. Check that recovery is otherwise side-effect free,
            # then finish the repository-level protocol configuration.
            self.forge.assert_seed_recovery_safe(
                resolved.intent.name, expected_tags=_seed_tags(resolved)
            )
            # The branch is already exact, but an interrupted seed (or a
            # deleted ref) may have left the pin tag missing. Repair it
            # inside a re-disabled window — the same hazard rules as the
            # original push — before enabling Actions.
            if _seed_tags(resolved):
                self.forge.disable_actions(resolved.intent.name)
                push_all_tags(
                    resolved,
                    self.forge.clone_url(resolved.intent.name),
                    self.forge.token,
                )
            self.forge.configure_repository(
                resolved.intent.name,
                resolved.intent.default_branch,
                resolved.intent.visibility,
            )
            self.forge.assert_repository_state(
                resolved.intent.name,
                expected_tags=_seed_tags(resolved),
                require_no_action_tasks=True,
                require_actions=False,
                expected_visibility=resolved.intent.visibility,
            )
            self.report(f"adopted exact existing seed for {resolved.intent.name}")
            return record

        self.forge.prepare_empty_repository(
            resolved.intent.name,
            resolved.intent.default_branch,
            resolved.intent.visibility,
            replace=replace,
        )
        with prepare_seed(resolved) as prepared:
            record = prepared_as_lock(prepared)
            remote_url = self.forge.clone_url(resolved.intent.name)
            push_seed(prepared, remote_url, self.forge.token)
            self.forge.configure_repository(
                resolved.intent.name,
                resolved.intent.default_branch,
                resolved.intent.visibility,
            )
            try:
                self.forge.wait_until_nonempty(resolved.intent.name)
            except WorldError:
                # Forgejo can keep `empty: true` briefly after accepting the
                # ref. Re-pushing was required in both live experiments; keep
                # the prepared object available until that read-back settles.
                self.report(
                    f"{resolved.intent.name} stayed empty after push; retrying ref"
                )
                push_seed(prepared, remote_url, self.forge.token)
                self.forge.wait_until_nonempty(resolved.intent.name)
        self.forge.assert_repository_state(
            resolved.intent.name,
            expected_tags=_seed_tags(resolved),
            require_no_action_tasks=True,
            require_actions=False,
            expected_visibility=resolved.intent.visibility,
        )
        self._verify_one(resolved, record)
        return record

    def _lock(
        self,
        records: list[WorldLockRepository],
        *,
        mode: str,
        existing: WorldLock | None,
    ) -> WorldLock:
        complete = len(records) == len(self.document.manifest.repos)
        timestamp = (
            existing.seeded_at or existing.adopted_at
            if existing is not None
            else self.now()
        )
        return WorldLock(
            status="complete" if complete else "partial",
            world_id=self.document.manifest.world_id,
            seeded_at=timestamp if mode == "seed" else None,
            adopted_at=timestamp if mode == "adopt" else None,
            forge=existing.forge if existing is not None else self.forge.base_url,
            org=self.document.manifest.org,
            manifest_path=(
                existing.manifest_path
                if existing is not None
                else _display_path(self.document.path)
            ),
            manifest_sha256=self.document.sha256,
            hub_commit=(
                existing.hub_commit if existing is not None else self.hub_commit
            ),
            repos=tuple(records),
        )


def _display_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return str(resolved)
