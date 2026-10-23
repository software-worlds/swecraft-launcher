"""Resolve upstream pins and prove live seed provenance with isolated Git."""

from __future__ import annotations

import re
import tempfile
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from swecraft_common import HubError
from swecraft_common.git import git_environment, run_git

from swecraft_world.errors import WorldError
from swecraft_world.models import (
    RELOCATE_PROTOCOL,
    STRIP_PROTOCOL,
    ManifestRepository,
    WorldLockRepository,
    WorldManifest,
)

_SHA = re.compile(r"[0-9a-f]{40}")
_STRIP_MESSAGE = """chore: remove upstream GitHub Actions workflows

These target github.com (hosted runners, marketplace actions, repository
secrets) and cannot work on this forge. Forgejo Actions is enabled here:
write workflows under .forgejo/workflows/ using a registered runner label.
"""

RELOCATED_WORKFLOWS_DIR = ".github/workflows-upstream"
RELOCATED_README_PATH = f"{RELOCATED_WORKFLOWS_DIR}/README.md"

_RELOCATE_MESSAGE = f"""chore: relocate upstream GitHub Actions workflows

Moved from .github/workflows/ to {RELOCATED_WORKFLOWS_DIR}/ so they stop
triggering (the forge reads .github/workflows too) while staying available
as reference. They target github.com and cannot run here. Forgejo Actions
is enabled: write workflows under .forgejo/workflows/ using a registered
runner label.
"""

_RELOCATED_README = """# Upstream GitHub CI (reference only)

The upstream repository's GitHub Actions workflows, preserved verbatim.
They cannot run on this forge: github.com is unreachable from jobs and
marketplace actions are unavailable. Use them as reference — test
matrices, lint steps, release flows — when writing runnable CI under
`.forgejo/workflows/` (runner labels: docker, ubuntu-latest, python).

## Writing CI that runs here

This forge's Actions is not GitHub's. Differences observed on this
forge family — verify with a small probe workflow before relying on
any of them:

- `uses: actions/...` steps fail (github.com is unreachable): run
  commands directly, and clone from this forge — the job's
  `$GITHUB_SERVER_URL` and `$GITHUB_REPOSITORY` point at it.
- The whole workflow graph is parsed at trigger time: one invalid
  workflow file can block every workflow, and duplicate YAML keys are
  fatal. Keep workflows few and simple.
- Reusable workflows are unreliable here: `if:` on a call job may be
  silently skipped, repository `vars` may not reach called workflows,
  and `needs` outputs may be invisible to step-level `if:`. Prefer
  plain single-file workflows with job-level `env`.
- Grow from one minimal green workflow; a broken one costs every
  future push a failed or missing run.
"""


def relocated_path(original: str) -> str:
    """Where the relocate transform puts one upstream workflow file."""

    relative = original.removeprefix(".github/workflows/")
    return f"{RELOCATED_WORKFLOWS_DIR}/{relative}"


def relocation_writes_explainer(stripped_paths: tuple[str, ...]) -> bool:
    """Whether the transform adds its own README next to the relocations.

    An upstream may ship `.github/workflows/README.md`; relocation then
    already places a README at the explainer's path, and overwriting it
    would both lose upstream content and break the declared-transform
    proof. In that case the upstream's own README stands. Computed purely
    from ``stripped_paths`` so preparation and verification always agree.
    """

    return all(
        relocated_path(original) != RELOCATED_README_PATH for original in stripped_paths
    )


def relocation_expected_added(stripped_paths: tuple[str, ...]) -> list[str]:
    """Every path the relocate transform adds, explainer included."""

    added = [relocated_path(original) for original in stripped_paths]
    if relocation_writes_explainer(stripped_paths):
        added.append(RELOCATED_README_PATH)
    return sorted(added)


@dataclass(frozen=True)
class ResolvedRepository:
    """Manifest intent with its upstream pin and transform inputs resolved."""

    intent: ManifestRepository
    cache: Path
    pin_commit: str
    pin_tree: str
    stripped_paths: tuple[str, ...]


@dataclass(frozen=True)
class PreparedSeed:
    """A temporary repository containing the exact baseline to push."""

    resolved: ResolvedRepository
    checkout: Path
    baseline_commit: str
    baseline_tree: str


@dataclass(frozen=True)
class LiveRepository:
    """A fetched live default branch and its current head."""

    checkout: Path
    current_head: str


def resolve_repositories(
    manifest: WorldManifest, cache_root: Path
) -> tuple[ResolvedRepository, ...]:
    """Resolve every pin before any caller mutates Forgejo."""

    root = cache_root / manifest.world_id
    resolved = tuple(_resolve(repository, root) for repository in manifest.repos)
    _validate_duplicate_pins(resolved)
    return resolved


def _resolve(repository: ManifestRepository, root: Path) -> ResolvedRepository:
    _check_ref(f"refs/heads/{repository.default_branch}", repository.name)
    if repository.pin.type == "tag":
        _check_ref(repository.pin.ref, repository.name)

    cache = root / f"{repository.name}.git"
    cache.parent.mkdir(parents=True, exist_ok=True)
    environment = git_environment(isolation_dir=root / ".git-environment")
    if cache.exists():
        if not (cache / "HEAD").is_file():
            raise WorldError(
                f"{repository.name}: cache is not a Git repository: {cache}"
            )
        origin = _git(
            ("remote", "get-url", "origin"), cwd=cache, env=environment
        ).strip()
        if origin != repository.upstream:
            raise WorldError(
                f"{repository.name}: cached origin is {origin!r}, expected "
                f"{repository.upstream!r}; remove {cache} or choose another cache"
            )
    else:
        _git(
            ("clone", "--mirror", "--", repository.upstream, str(cache)),
            cwd=cache.parent,
            env=environment,
            context=repository.name,
        )
    _git(
        ("fetch", "--prune", "--force", "--tags", "origin"),
        cwd=cache,
        env=environment,
        context=repository.name,
    )

    candidate = f"{repository.pin.ref}^{{commit}}"
    try:
        pin_commit = _git(
            ("rev-parse", "--verify", candidate),
            cwd=cache,
            env=environment,
            context=repository.name,
        ).strip()
    except WorldError:
        if repository.pin.type != "commit":
            raise
        _git(
            ("fetch", "--force", "origin", repository.pin.value),
            cwd=cache,
            env=environment,
            context=repository.name,
        )
        pin_commit = _git(
            ("rev-parse", "--verify", candidate),
            cwd=cache,
            env=environment,
            context=repository.name,
        ).strip()
    _require_sha(pin_commit, f"{repository.name}: resolved pin")
    pin_tree = _git(
        ("rev-parse", f"{pin_commit}^{{tree}}"),
        cwd=cache,
        env=environment,
        context=repository.name,
    ).strip()
    _require_sha(pin_tree, f"{repository.name}: pin tree")
    listing = _git(
        (
            "ls-tree",
            "-r",
            "--name-only",
            pin_commit,
            "--",
            ".github/workflows",
        ),
        cwd=cache,
        env=environment,
        context=repository.name,
    )
    stripped_paths = tuple(sorted(line for line in listing.splitlines() if line))
    return ResolvedRepository(
        intent=repository,
        cache=cache,
        pin_commit=pin_commit,
        pin_tree=pin_tree,
        stripped_paths=stripped_paths,
    )


def _validate_duplicate_pins(resolved: tuple[ResolvedRepository, ...]) -> None:
    by_name = {repository.intent.name: repository for repository in resolved}
    by_commit: dict[str, list[ResolvedRepository]] = defaultdict(list)
    for repository in resolved:
        by_commit[repository.pin_commit].append(repository)
        target_name = repository.intent.duplicate_pin_of
        if target_name is not None:
            target = by_name[target_name]
            if target.pin_commit != repository.pin_commit:
                raise WorldError(
                    f"{repository.intent.name}: duplicate_pin_of {target_name} "
                    "resolved to a different commit"
                )
    for commit, repositories in by_commit.items():
        if len(repositories) == 1:
            if repositories[0].intent.duplicate_pin_of is not None:
                raise WorldError(
                    f"{repositories[0].intent.name}: duplicate_pin_of was declared "
                    f"but no other repository resolved to {commit}"
                )
            continue
        primaries = [
            repository
            for repository in repositories
            if repository.intent.duplicate_pin_of is None
        ]
        if len(primaries) != 1:
            names = ", ".join(repository.intent.name for repository in repositories)
            raise WorldError(
                f"duplicate pin commit {commit} across {names}; declare one primary"
            )
        primary = primaries[0]
        for repository in repositories:
            if (
                repository is not primary
                and repository.intent.duplicate_pin_of != primary.intent.name
            ):
                raise WorldError(
                    f"{repository.intent.name}: duplicate_pin_of must be "
                    f"{primary.intent.name}"
                )


@contextmanager
def prepare_seed(resolved: ResolvedRepository) -> Iterator[PreparedSeed]:
    """Build the seed baseline in a disposable clone of the upstream cache."""

    with tempfile.TemporaryDirectory(
        prefix=f"swecraft-world-{resolved.intent.name}-"
    ) as raw:
        root = Path(raw)
        checkout = root / "repository"
        environment = git_environment(isolation_dir=root / ".git-environment")
        _git(
            ("clone", "--no-checkout", str(resolved.cache), str(checkout)),
            cwd=root,
            env=environment,
            context=resolved.intent.name,
        )
        # A plain clone only carries tags on the cloned branch's history;
        # fetch the mirror's full tag set so the seed push can preserve the
        # upstream's release history, side branches included. The push
        # itself selects from this set — see `seed_tags` — and withholds
        # the pin's own future.
        _git(
            ("fetch", "origin", "refs/tags/*:refs/tags/*"),
            cwd=checkout,
            env=environment,
            context=resolved.intent.name,
        )
        _git(
            ("checkout", "--detach", resolved.pin_commit),
            cwd=checkout,
            env=environment,
            context=resolved.intent.name,
        )
        baseline_commit = resolved.pin_commit
        baseline_tree = resolved.pin_tree
        if resolved.stripped_paths:
            protocol = resolved.intent.seed_protocol
            if protocol not in (STRIP_PROTOCOL, RELOCATE_PROTOCOL):
                raise WorldError(
                    f"{resolved.intent.name}: unknown seed protocol {protocol!r}"
                )
            if protocol == STRIP_PROTOCOL:
                _git(
                    ("rm", "-r", "--", ".github/workflows"),
                    cwd=checkout,
                    env=environment,
                    context=resolved.intent.name,
                )
                message = _STRIP_MESSAGE
            else:
                for original in resolved.stripped_paths:
                    target = checkout / relocated_path(original)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    _git(
                        ("mv", "--", original, relocated_path(original)),
                        cwd=checkout,
                        env=environment,
                        context=resolved.intent.name,
                    )
                if relocation_writes_explainer(resolved.stripped_paths):
                    readme = checkout / RELOCATED_README_PATH
                    readme.write_text(_RELOCATED_README, encoding="utf-8")
                    _git(
                        ("add", "--", RELOCATED_README_PATH),
                        cwd=checkout,
                        env=environment,
                        context=resolved.intent.name,
                    )
                message = _RELOCATE_MESSAGE
            # The seed commit's identity is part of the seed protocol: it fixes the
            # baseline commit's id, so it keeps the name worlds were first seeded
            # with, and the paper's locks still verify.
            commit_environment = {
                **environment,
                "GIT_AUTHOR_NAME": "hub-admin",
                "GIT_AUTHOR_EMAIL": "hub-admin@localhost",
                "GIT_COMMITTER_NAME": "hub-admin",
                "GIT_COMMITTER_EMAIL": "hub-admin@localhost",
            }
            _git(
                ("commit", "-m", message),
                cwd=checkout,
                env=commit_environment,
                context=resolved.intent.name,
            )
            baseline_commit = _git(
                ("rev-parse", "HEAD"),
                cwd=checkout,
                env=environment,
                context=resolved.intent.name,
            ).strip()
            baseline_tree = _git(
                ("rev-parse", "HEAD^{tree}"),
                cwd=checkout,
                env=environment,
                context=resolved.intent.name,
            ).strip()
        prepared = PreparedSeed(
            resolved=resolved,
            checkout=checkout,
            baseline_commit=baseline_commit,
            baseline_tree=baseline_tree,
        )
        verify_transform(checkout, prepared_as_lock(prepared))
        yield prepared


def push_seed(prepared: PreparedSeed, remote_url: str, token: str) -> None:
    """Push the prepared baseline branch and the seed's tag set.

    The tag set is the upstream's history up to the pin, never the pin's
    own future — see `future_tags`. Tags are pushed as explicit refspecs
    rather than `refs/tags/*` so a later tag cannot ride along unnoticed.

    The caller must hold the repository's Actions unit disabled: upstream
    tags' trees still carry `.github/workflows`, and pushing a tag while
    Actions is enabled queues those workflows (observed live and
    reproduced on Forgejo 15). Pushed while
    disabled, enabling Actions afterwards queues nothing — proven by the
    same probe — and the post-enable zero-tasks assertion is the standing
    tripwire. Re-pushing identical tag refs is a no-op, so the
    nonempty-retry path stays safe after Actions is enabled.
    """

    environment = git_environment(
        isolation_dir=prepared.checkout.parent / ".forge-git-environment",
        token=token,
        remote_url=remote_url,
    )
    branch = prepared.resolved.intent.default_branch
    # Force applies per refspec: the branch may be re-seeded, but tags are
    # never forced — an existing identical tag is a no-op, a divergent one
    # is tampering and must fail loudly.
    references = [
        f"+{prepared.baseline_commit}:refs/heads/{branch}",
        *_tag_refspecs(prepared.resolved),
    ]
    _git(
        (
            "push",
            # Atomic: branch and tag land together or not at all, so an
            # interrupted push can never strand a branch without its tag.
            "--atomic",
            "--no-follow-tags",
            remote_url,
            *references,
        ),
        cwd=prepared.checkout,
        env=environment,
        context=prepared.resolved.intent.name,
    )


def push_all_tags(resolved: ResolvedRepository, remote_url: str, token: str) -> None:
    """Repair the seed's tag set from the cache during seed recovery.

    Pushes exactly what `seed_tags` allows, so recovery cannot reintroduce
    a future tag that the original seed correctly withheld.

    Idempotent: identical existing tags are no-ops; a divergent one fails
    loudly. The caller must hold Actions disabled, exactly as for the
    original seed push.
    """

    with tempfile.TemporaryDirectory(prefix="swecraft-world-tag-") as raw:
        environment = git_environment(
            isolation_dir=Path(raw) / ".git-environment",
            token=token,
            remote_url=remote_url,
        )
        _git(
            ("push", "--no-follow-tags", remote_url, *_tag_refspecs(resolved)),
            cwd=resolved.cache,
            env=environment,
            context=resolved.intent.name,
        )


def cache_tags(resolved: ResolvedRepository) -> dict[str, str]:
    """The upstream's full tag set as {name: peeled commit}. The seed
    pushes and verifies a subset of it — see `seed_tags`."""

    with tempfile.TemporaryDirectory(prefix="swecraft-world-tags-") as raw:
        environment = git_environment(isolation_dir=Path(raw) / ".git-environment")
        # lstrip=2, not :short — short is a display name and emits
        # "tags/v1" when a branch and tag share a name, which would make
        # the exactness assertion reject a correctly pushed seed.
        layout = "%(refname:lstrip=2) %(*objectname)%(objectname)"
        listing = _git(
            ("for-each-ref", f"--format={layout}", "refs/tags"),
            cwd=resolved.cache,
            env=environment,
            context=resolved.intent.name,
        )
    tags: dict[str, str] = {}
    for line in listing.splitlines():
        tag, _, sha = line.partition(" ")
        if tag and sha:
            tags[tag] = sha[:40]
    return tags


def future_tags(resolved: ResolvedRepository) -> dict[str, str]:
    """Tags the world must not carry: the pin's own future.

    A tag qualifies when the pin commit is a strict ancestor of it — that
    is, the tag is a later release of the very line the seed pins. Nothing
    else counts. Version ordering is not the test (urllib3's 1.26.x tags
    sit on a maintenance branch, ahead of nothing, and are legitimate
    history a maintainer should see), and neither are commit dates, which
    `relocate-github-actions-v1` rewrites to the seeding moment.

    Seeding a repository with its own future makes the world unwinnable in
    a way agents are right to detect: in one of our runs, urllib3
    was pinned at 2.7.0 while the 2.8.0 tag rode along 69 commits ahead,
    already carrying the URL percent-encoding optimization its maintainer
    then spent the run rediscovering. The agent measured a real gain
    against the seeded baseline, compared against the imported tag, and
    correctly refused to ship work that beat only a stale reference —
    after which it idled for 119 wakes.
    """

    tags = cache_tags(resolved)
    with tempfile.TemporaryDirectory(prefix="swecraft-world-future-") as raw:
        environment = git_environment(isolation_dir=Path(raw) / ".git-environment")
        listing = _git(
            ("tag", "--contains", resolved.pin_commit),
            cwd=resolved.cache,
            env=environment,
            context=resolved.intent.name,
        )
    descendants = {line.strip() for line in listing.splitlines() if line.strip()}
    # `--contains` reports the pin's own tag too; that one is the seed, not
    # the future, so it is matched by commit rather than by name.
    return {
        tag: sha
        for tag, sha in tags.items()
        if tag in descendants and sha != resolved.pin_commit
    }


def seed_tags(resolved: ResolvedRepository) -> dict[str, str]:
    """Exactly the tags the seed pushes: the upstream's history up to the
    pin, its side branches included, and none of the pin's own future."""

    excluded = future_tags(resolved)
    return {
        tag: sha for tag, sha in cache_tags(resolved).items() if tag not in excluded
    }


def _tag_refspecs(resolved: ResolvedRepository) -> list[str]:
    """Explicit per-tag refspecs, so a wildcard cannot smuggle the future in."""

    return [f"refs/tags/{tag}:refs/tags/{tag}" for tag in sorted(seed_tags(resolved))]


@contextmanager
def fetch_live(
    resolved: ResolvedRepository, remote_url: str, token: str
) -> Iterator[LiveRepository]:
    """Fetch one live default branch without tags into a disposable repository."""

    with tempfile.TemporaryDirectory(
        prefix=f"swecraft-world-live-{resolved.intent.name}-"
    ) as raw:
        root = Path(raw)
        checkout = root / "repository"
        checkout.mkdir()
        environment = git_environment(
            isolation_dir=root / ".git-environment",
            token=token,
            remote_url=remote_url,
        )
        _git(
            ("init", "--bare"),
            cwd=checkout,
            env=environment,
            context=resolved.intent.name,
        )
        branch = resolved.intent.default_branch
        live_ref = f"refs/remotes/origin/{branch}"
        _git(
            (
                "fetch",
                "--no-tags",
                remote_url,
                f"+refs/heads/{branch}:{live_ref}",
            ),
            cwd=checkout,
            env=environment,
            context=resolved.intent.name,
        )
        current_head = _git(
            ("rev-parse", live_ref),
            cwd=checkout,
            env=environment,
            context=resolved.intent.name,
        ).strip()
        _require_sha(current_head, f"{resolved.intent.name}: live head")
        yield LiveRepository(checkout=checkout, current_head=current_head)


def verify_transform(checkout: Path, record: WorldLockRepository) -> None:
    """Prove pin and baseline trees and the complete v1 transform diff."""

    environment = git_environment(isolation_dir=checkout.parent / ".verify-git-env")
    for commit, expected_tree, label in (
        (record.pin_commit, record.pin_tree, "pin"),
        (record.baseline_commit, record.baseline_tree, "baseline"),
    ):
        actual_tree = _git(
            ("rev-parse", f"{commit}^{{tree}}"),
            cwd=checkout,
            env=environment,
            context=record.name,
        ).strip()
        if actual_tree != expected_tree:
            raise WorldError(
                f"{record.name}: {label} tree is {actual_tree}, expected "
                f"{expected_tree}"
            )
    if not record.stripped_paths:
        if record.baseline_commit != record.pin_commit:
            raise WorldError(f"{record.name}: untransformed baseline changed the pin")
        return
    parents = _git(
        ("rev-list", "--parents", "-n", "1", record.baseline_commit),
        cwd=checkout,
        env=environment,
        context=record.name,
    ).split()
    if len(parents) != 2 or parents[1] != record.pin_commit:
        raise WorldError(
            f"{record.name}: transformed baseline must be the direct child of its pin"
        )
    changed = _git(
        (
            "diff",
            "--name-status",
            "--no-renames",
            record.pin_commit,
            record.baseline_commit,
        ),
        cwd=checkout,
        env=environment,
        context=record.name,
    )
    deleted: list[str] = []
    added: list[str] = []
    unexpected: list[str] = []
    for line in changed.splitlines():
        status, separator, path = line.partition("\t")
        if status == "D" and separator and path:
            deleted.append(path)
        elif status == "A" and separator and path:
            added.append(path)
        else:
            unexpected.append(line)
    if record.seed_protocol not in (STRIP_PROTOCOL, RELOCATE_PROTOCOL):
        raise WorldError(
            f"{record.name}: unknown seed protocol {record.seed_protocol!r}"
        )
    if record.seed_protocol == STRIP_PROTOCOL:
        if unexpected or added or tuple(sorted(deleted)) != record.stripped_paths:
            raise WorldError(
                f"{record.name}: baseline transform differs from declared "
                f"workflow deletions (unexpected={unexpected + added}, "
                f"deleted={sorted(deleted)})"
            )
        return
    expected_added = relocation_expected_added(record.stripped_paths)
    if (
        unexpected
        or tuple(sorted(deleted)) != record.stripped_paths
        or sorted(added) != expected_added
    ):
        raise WorldError(
            f"{record.name}: baseline transform differs from declared "
            f"workflow relocation (unexpected={unexpected}, "
            f"deleted={sorted(deleted)}, added={sorted(added)})"
        )
    for original in record.stripped_paths:
        source_blob = _git(
            ("rev-parse", f"{record.pin_commit}:{original}"),
            cwd=checkout,
            env=environment,
            context=record.name,
        ).strip()
        target_blob = _git(
            ("rev-parse", f"{record.baseline_commit}:{relocated_path(original)}"),
            cwd=checkout,
            env=environment,
            context=record.name,
        ).strip()
        if source_blob != target_blob:
            raise WorldError(
                f"{record.name}: relocated workflow {original} does not match "
                "its upstream content"
            )


def verify_live_record(live: LiveRepository, record: WorldLockRepository) -> None:
    """Prove a lock entry against fetched Git objects and current ancestry."""

    verify_transform(live.checkout, record)
    environment = git_environment(
        isolation_dir=live.checkout.parent / ".ancestry-git-env"
    )
    try:
        _git(
            ("merge-base", "--is-ancestor", record.baseline_commit, live.current_head),
            cwd=live.checkout,
            env=environment,
            context=record.name,
        )
    except WorldError as error:
        raise WorldError(
            f"{record.name}: baseline {record.baseline_commit} is not an ancestor "
            f"of live head {live.current_head}"
        ) from error


def discover_adoption_baseline(
    live: LiveRepository, resolved: ResolvedRepository
) -> tuple[str, str]:
    """Locate the original seed baseline on the live first-parent history."""

    environment = git_environment(isolation_dir=live.checkout.parent / ".adopt-git-env")
    try:
        _git(
            (
                "merge-base",
                "--is-ancestor",
                resolved.pin_commit,
                live.current_head,
            ),
            cwd=live.checkout,
            env=environment,
            context=resolved.intent.name,
        )
    except WorldError as error:
        raise WorldError(
            f"{resolved.intent.name}: pin {resolved.pin_commit} is not an ancestor "
            f"of live head {live.current_head}; refusing seed recovery"
        ) from error
    if not resolved.stripped_paths:
        baseline = resolved.pin_commit
    else:
        descendants = _git(
            (
                "rev-list",
                "--first-parent",
                "--reverse",
                f"{resolved.pin_commit}..{live.current_head}",
            ),
            cwd=live.checkout,
            env=environment,
            context=resolved.intent.name,
        ).splitlines()
        if not descendants:
            raise WorldError(
                f"{resolved.intent.name}: live history has no seed transform after pin"
            )
        baseline = descendants[0]
    baseline_tree = _git(
        ("rev-parse", f"{baseline}^{{tree}}"),
        cwd=live.checkout,
        env=environment,
        context=resolved.intent.name,
    ).strip()
    record = lock_record(
        resolved,
        baseline_commit=baseline,
        baseline_tree=baseline_tree,
        current_head=live.current_head,
        adopted=True,
        # This record exists only for verify_live_record, which checks the
        # Git transform and ancestry and never reads actions_enabled. The
        # caller writes the real lock entry with the observed value.
        actions_enabled=True,
    )
    verify_live_record(live, record)
    return baseline, baseline_tree


def lock_record(
    resolved: ResolvedRepository,
    *,
    baseline_commit: str,
    baseline_tree: str,
    current_head: str | None = None,
    adopted: bool = False,
    actions_enabled: bool = False,
) -> WorldLockRepository:
    """Create one structurally validated lock entry.

    ``actions_enabled`` is an observation of the repository at the moment the
    entry is written, not a target. Seeding leaves it False for the whole
    world and `swecraft-world enable-actions` rewrites the lock once Actions is
    genuinely on; adoption records the live state of a world that already has
    it. Defaulting to False keeps the seed path -- the one that must not
    enable Actions -- correct without an argument at every call site.
    """

    return WorldLockRepository(
        name=resolved.intent.name,
        upstream=resolved.intent.upstream,
        pin=resolved.intent.pin,
        visibility=resolved.intent.visibility,
        pin_ref=resolved.intent.pin.ref,
        pin_commit=resolved.pin_commit,
        pin_tree=resolved.pin_tree,
        default_branch=resolved.intent.default_branch,
        seed_protocol=resolved.intent.seed_protocol,
        baseline_commit=baseline_commit,
        baseline_tree=baseline_tree,
        stripped_paths=resolved.stripped_paths,
        seed_tag=(
            resolved.intent.pin.value if resolved.intent.pin.type == "tag" else None
        ),
        actions_enabled=actions_enabled,
        duplicate_pin_of=resolved.intent.duplicate_pin_of,
        current_head_at_adoption=(
            current_head
            if adopted and current_head is not None and current_head != baseline_commit
            else None
        ),
    )


def prepared_as_lock(prepared: PreparedSeed) -> WorldLockRepository:
    return lock_record(
        prepared.resolved,
        baseline_commit=prepared.baseline_commit,
        baseline_tree=prepared.baseline_tree,
    )


def _check_ref(ref: str, name: str) -> None:
    with tempfile.TemporaryDirectory(prefix="swecraft-world-ref-") as raw:
        environment = git_environment(isolation_dir=Path(raw) / ".git-environment")
        _git(("check-ref-format", ref), cwd=Path(raw), env=environment, context=name)


def _require_sha(value: str, label: str) -> None:
    if not _SHA.fullmatch(value):
        raise WorldError(f"{label} is not a full lowercase Git object ID: {value!r}")


def _git(
    arguments: tuple[str, ...],
    *,
    cwd: Path,
    env: dict[str, str],
    context: str = "world",
) -> str:
    try:
        return run_git(arguments, cwd=cwd, env=env)
    except HubError as error:
        raise WorldError(f"{context}: {error}") from error
