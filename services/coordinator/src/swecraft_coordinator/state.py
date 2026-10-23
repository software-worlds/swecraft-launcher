"""Small SQLite store: one active wake per agent, due times, daily usage.

Completions are fenced: ``claim`` records the active wake id, and
``finish``/``fail`` only mutate the agent's scheduler state while that id
is still the active one. A worker that lost its claim to a stale-takeover
can finalize its own receipt but can no longer move the agent's state.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from swecraft_common.lifecycle import LifecycleBusy, society_lifecycle_lock

from swecraft_coordinator import CoordinatorError

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_state (
    agent TEXT PRIMARY KEY,
    active_started TEXT,
    active_wake_id INTEGER,
    next_due TEXT NOT NULL,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_run_id TEXT,
    last_outcome TEXT,
    last_summary TEXT,
    last_finished TEXT,
    last_error TEXT,
    last_uncertain INTEGER NOT NULL DEFAULT 0,
    bootstrapped_at TEXT
);
CREATE TABLE IF NOT EXISTS wakes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT NOT NULL,
    day TEXT NOT NULL,
    started TEXT NOT NULL,
    finished TEXT,
    run_id TEXT,
    status TEXT NOT NULL,
    outcome TEXT,
    summary TEXT,
    cost_usd REAL NOT NULL DEFAULT 0,
    error TEXT
);
CREATE INDEX IF NOT EXISTS wakes_agent_day ON wakes (agent, day);
CREATE TABLE IF NOT EXISTS checkpoint_marker (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    wake_count INTEGER NOT NULL,
    recorded TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_repos (
    agent TEXT NOT NULL,
    repo TEXT NOT NULL,
    PRIMARY KEY (agent, repo)
);
CREATE TABLE IF NOT EXISTS budget_ledger (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    sha TEXT NOT NULL,
    allocation TEXT NOT NULL,
    applied TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wake_treatments (
    wake_id INTEGER PRIMARY KEY,
    treatment TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS evaluation_marker (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    wake_count INTEGER NOT NULL,
    recorded TEXT NOT NULL
);
"""


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


@dataclass(frozen=True)
class ActivityBucket:
    """One time bucket of the wake ledger, counted by verdict."""

    start: str
    worked: int
    idle: int
    failed: int
    running: int
    cost_usd: float

    @property
    def total(self) -> int:
        return self.worked + self.idle + self.failed + self.running


@dataclass(frozen=True)
class Wake:
    """One row of the wake ledger, as read back for reporting.

    ``status`` is the wake's own lifecycle — ``running`` while in flight, then
    ``completed`` / ``failed``, or ``interrupted`` / ``cancelled`` /
    ``superseded`` when it lost its claim. ``outcome`` is the agent's own
    advisory verdict and is only ever set on a completed wake.
    """

    id: int
    agent: str
    day: str
    started: str
    finished: str | None
    run_id: str | None
    status: str
    outcome: str | None
    summary: str | None
    cost_usd: float
    error: str | None


@dataclass(frozen=True)
class AgentStatus:
    agent: str
    active_started: str | None
    active_wake_id: int | None
    next_due: str
    consecutive_failures: int
    last_run_id: str | None
    last_outcome: str | None
    last_summary: str | None
    last_finished: str | None
    last_error: str | None
    last_uncertain: bool
    bootstrapped_at: str | None


class StateStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=30)
        connection.row_factory = sqlite3.Row
        # Concurrent wakes claim and record from separate threads: WAL lets a
        # reader proceed while another connection writes, and the timeout
        # above absorbs the brief writer-versus-writer overlap.
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def ensure_agent(self, agent: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO agent_state (agent, next_due) VALUES (?, ?)",
                (agent, _iso(_now())),
            )

    def claim(self, agent: str, *, stale_after_seconds: int) -> int:
        """Wait for the society lifecycle lock, then start one wake."""

        try:
            with society_lifecycle_lock(self._path):
                return self._claim(agent, stale_after_seconds=stale_after_seconds)
        except LifecycleBusy:
            logger.info("society lifecycle is busy; waiting to claim %s", agent)
        with society_lifecycle_lock(self._path, timeout_seconds=-1):
            return self._claim(agent, stale_after_seconds=stale_after_seconds)

    def _claim(self, agent: str, *, stale_after_seconds: int) -> int:
        """Claim transaction, including stale-owner fencing."""

        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT active_started FROM agent_state WHERE agent = ?", (agent,)
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO agent_state (agent, next_due) VALUES (?, ?)",
                    (agent, _iso(now)),
                )
            elif row["active_started"] is not None:
                started = datetime.fromisoformat(row["active_started"])
                if (now - started).total_seconds() < stale_after_seconds:
                    raise CoordinatorError(
                        f"agent {agent} already has an active wake "
                        f"(started {row['active_started']})"
                    )
                connection.execute(
                    "UPDATE wakes SET status = 'interrupted', finished = ? "
                    "WHERE agent = ? AND status = 'running'",
                    (_iso(now), agent),
                )
                connection.execute(
                    "UPDATE agent_state SET last_uncertain = 1, last_error = "
                    "'a previous wake was interrupted before finishing' "
                    "WHERE agent = ?",
                    (agent,),
                )
            cursor = connection.execute(
                "INSERT INTO wakes (agent, day, started, status) "
                "VALUES (?, ?, ?, 'running')",
                (agent, now.strftime("%Y-%m-%d"), _iso(now)),
            )
            wake_id = int(cursor.lastrowid or 0)
            connection.execute(
                "UPDATE agent_state SET active_started = ?, active_wake_id = ? "
                "WHERE agent = ?",
                (_iso(now), wake_id, agent),
            )
            return wake_id

    def finish(
        self,
        agent: str,
        wake_id: int,
        *,
        run_id: str | None,
        outcome: str | None,
        summary: str | None,
        cost_usd: float,
        next_due_seconds: int,
    ) -> bool:
        """Record a completed wake; returns False when fenced as stale."""

        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE wakes SET finished = ?, run_id = ?, status = 'completed', "
                "outcome = ?, summary = ?, cost_usd = ? WHERE id = ?",
                (_iso(now), run_id, outcome, summary, cost_usd, wake_id),
            )
            cursor = connection.execute(
                "UPDATE agent_state SET active_started = NULL, "
                "active_wake_id = NULL, next_due = ?, consecutive_failures = 0, "
                "last_run_id = ?, last_outcome = ?, last_summary = ?, "
                "last_finished = ?, last_error = NULL, last_uncertain = 0 "
                "WHERE agent = ? AND active_wake_id = ?",
                (
                    _iso(_due(now, next_due_seconds)),
                    run_id,
                    outcome,
                    summary,
                    _iso(now),
                    agent,
                    wake_id,
                ),
            )
            if cursor.rowcount == 1:
                return True
            connection.execute(
                "UPDATE wakes SET status = 'superseded' WHERE id = ?", (wake_id,)
            )
            logger.warning(
                "wake %d for %s finished after losing its claim; "
                "scheduler state untouched",
                wake_id,
                agent,
            )
            return False

    def fail(
        self,
        agent: str,
        wake_id: int,
        *,
        error: str,
        run_id: str | None,
        cost_usd: float,
        next_due_seconds: int,
    ) -> bool:
        """Record a failed wake; returns False when fenced as stale.

        A failure may have happened after external side effects, so the
        agent is marked uncertain and the error retained for the next
        wake's prompt.
        """

        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE wakes SET finished = ?, run_id = ?, status = 'failed', "
                "cost_usd = ?, error = ? WHERE id = ?",
                (_iso(now), run_id, cost_usd, error[:500], wake_id),
            )
            cursor = connection.execute(
                "UPDATE agent_state SET active_started = NULL, "
                "active_wake_id = NULL, next_due = ?, "
                "consecutive_failures = consecutive_failures + 1, "
                "last_finished = ?, last_error = ?, last_uncertain = 1 "
                "WHERE agent = ? AND active_wake_id = ?",
                (
                    _iso(_due(now, next_due_seconds)),
                    _iso(now),
                    error[:500],
                    agent,
                    wake_id,
                ),
            )
            if cursor.rowcount == 1:
                return True
            connection.execute(
                "UPDATE wakes SET status = 'superseded' WHERE id = ?", (wake_id,)
            )
            logger.warning(
                "wake %d for %s failed after losing its claim; "
                "scheduler state untouched",
                wake_id,
                agent,
            )
            return False

    def recover(self, agent: str) -> bool:
        """Release the claim a dead wake left behind; returns False if none.

        For operators, after a killed scheduler or wake process: the claim
        would otherwise block the agent until it goes stale (its timeout
        plus the margin). The cancelled wake is recorded as an operator
        action, not an agent failure — no backoff is charged — but the
        agent is marked uncertain, because the dead wake may have acted on
        the forge before dying. Only use it once the wake's process and
        trial container are actually gone; releasing a live wake recreates
        the double-claim the staleness window exists to prevent.
        """

        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT active_wake_id FROM agent_state WHERE agent = ?",
                (agent,),
            ).fetchone()
            if row is None or row["active_wake_id"] is None:
                return False
            connection.execute(
                "UPDATE wakes SET status = 'cancelled', finished = ?, "
                "error = 'claim released by operator recovery' WHERE id = ?",
                (_iso(now), row["active_wake_id"]),
            )
            connection.execute(
                "UPDATE agent_state SET active_started = NULL, "
                "active_wake_id = NULL, next_due = ?, last_uncertain = 1, "
                "last_error = 'a previous wake was cancelled by operator "
                "recovery before finishing' WHERE agent = ?",
                (_iso(_due(now, 60)), agent),
            )
            return True

    def mark_bootstrapped(self, agent: str) -> None:
        self.ensure_agent(agent)
        with self._connect() as connection:
            connection.execute(
                "UPDATE agent_state SET bootstrapped_at = ? WHERE agent = ?",
                (_iso(_now()), agent),
            )

    def is_bootstrapped(self, agent: str) -> bool:
        status = self.status(agent)
        return status is not None and status.bootstrapped_at is not None

    def status(self, agent: str) -> AgentStatus | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_state WHERE agent = ?", (agent,)
            ).fetchone()
        if row is None:
            return None
        return AgentStatus(
            agent=row["agent"],
            active_started=row["active_started"],
            active_wake_id=row["active_wake_id"],
            next_due=row["next_due"],
            consecutive_failures=row["consecutive_failures"],
            last_run_id=row["last_run_id"],
            last_outcome=row["last_outcome"],
            last_summary=row["last_summary"],
            last_finished=row["last_finished"],
            last_error=row["last_error"],
            last_uncertain=bool(row["last_uncertain"]),
            bootstrapped_at=row["bootstrapped_at"],
        )

    def known_agents(self) -> list[str]:
        """Every agent the store has seen, including ones since unconfigured.

        Reporting joins these against the configured profiles so an agent
        removed or renamed in ``coordinator.yaml`` keeps its history instead
        of silently disappearing.
        """

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT agent FROM agent_state ORDER BY agent"
            ).fetchall()
        return [row["agent"] for row in rows]

    def activity(
        self,
        *,
        bucket_seconds: int = 3_600,
        agents: list[str] | None = None,
    ) -> list[ActivityBucket]:
        """Every bucket of the ledger, counted by verdict — the whole span.

        The feed cannot answer this. ``list_wakes`` returns rows and is capped,
        so a caller wanting the shape of a campaign would page a ledger that
        already runs past the cap: Exp7 is 868 wakes against a 500 limit, and
        a chart drawn from that page starts 42% in while claiming to start at
        the beginning. Counting in SQL returns one row per bucket instead —
        76 rows for a three-day campaign — so the range is complete and small
        at the same time.

        ``agents`` narrows in SQL rather than after the fact. A repository
        filter reaches here already translated to its agents, because the
        binding lives in configuration and the ledger stores only a name;
        filtering afterwards is what forces the feed to over-fetch.
        """

        seconds = max(60, int(bucket_seconds))
        clauses: list[str] = []
        parameters: list[object] = []
        if agents is not None:
            if not agents:
                return []
            clauses.append(f"agent IN ({','.join('?' for _ in agents)})")
            parameters.extend(agents)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        # strftime('%s') on the stored ISO timestamp, floored to the bucket.
        bucket_sql = (
            f"(CAST(strftime('%s', started) AS INTEGER) / {seconds}) * {seconds}"
        )
        sql = f"""
            SELECT {bucket_sql} AS bucket,
                   SUM(CASE WHEN status = 'completed' AND outcome = 'worked'
                            THEN 1 ELSE 0 END) AS worked,
                   SUM(CASE WHEN status = 'completed' AND (outcome IS NULL
                            OR outcome != 'worked') THEN 1 ELSE 0 END) AS idle,
                   SUM(CASE WHEN status NOT IN ('completed', 'running')
                            THEN 1 ELSE 0 END) AS failed,
                   SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running,
                   COALESCE(SUM(cost_usd), 0) AS cost_usd
            FROM wakes{where}
            GROUP BY bucket
            ORDER BY bucket
        """
        with self._connect() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [
            ActivityBucket(
                start=datetime.fromtimestamp(row["bucket"], UTC).isoformat(),
                worked=row["worked"] or 0,
                idle=row["idle"] or 0,
                failed=row["failed"] or 0,
                running=row["running"] or 0,
                cost_usd=float(row["cost_usd"] or 0.0),
            )
            for row in rows
        ]

    def list_wakes(
        self,
        *,
        agent: str | None = None,
        status: str | None = None,
        run_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 50,
    ) -> list[Wake]:
        """Most recent wakes first, optionally narrowed.

        ``run_id`` narrows in SQL rather than after the fact. That matters
        because the caller is not paging a feed — it is looking up one known
        wake — and filtering a page of the newest rows would silently lose
        any session older than that page.

        ``since``/``until`` are a half-open interval, ``since <= started <
        until``. That is the shape of a chart bucket, so a caller selecting one
        bar gets exactly its wakes: closed at both ends, a wake landing on a
        boundary would appear under both neighbouring bars and be counted
        twice. Comparison goes through ``strftime`` rather than string ordering
        because the caller supplies the bound and need not have written it in
        the same offset the ledger stores.
        """

        clauses = []
        parameters: list[object] = []
        if agent is not None:
            clauses.append("agent = ?")
            parameters.append(agent)
        if status is not None:
            clauses.append("status = ?")
            parameters.append(status)
        if run_id is not None:
            clauses.append("run_id = ?")
            parameters.append(run_id)
        if since is not None:
            clauses.append("strftime('%s', started) >= strftime('%s', ?)")
            parameters.append(since)
        if until is not None:
            clauses.append("strftime('%s', started) < strftime('%s', ?)")
            parameters.append(until)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(max(1, int(limit)))
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM wakes{where} ORDER BY id DESC LIMIT ?", parameters
            ).fetchall()
        return [
            Wake(
                id=row["id"],
                agent=row["agent"],
                day=row["day"],
                started=row["started"],
                finished=row["finished"],
                run_id=row["run_id"],
                status=row["status"],
                outcome=row["outcome"],
                summary=row["summary"],
                cost_usd=float(row["cost_usd"]),
                error=row["error"],
            )
            for row in rows
        ]

    def is_due(self, agent: str) -> bool:
        status = self.status(agent)
        if status is None:
            return True
        if status.active_started is not None:
            return False
        return datetime.fromisoformat(status.next_due) <= _now()

    def completed_timeline(self) -> list[tuple[str, str | None, float]]:
        """Every completed wake's ``(finished, outcome, cost_usd)``, oldest first.

        One query for the whole ledger, because a measurement's place in the
        campaign — how many wakes had finished by the time it started, and
        what they had cost — is answered by bisecting this list, and a series
        asks that once per reading.
        """

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT finished, outcome, cost_usd FROM wakes "
                "WHERE status = 'completed' AND finished IS NOT NULL "
                "ORDER BY finished, id"
            ).fetchall()
        return [
            (row["finished"], row["outcome"], float(row["cost_usd"] or 0.0))
            for row in rows
        ]

    def completed_wakes(self) -> int:
        """Completed wakes over the society's whole history — its step count."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM wakes WHERE status = 'completed'"
            ).fetchone()
        return int(row[0])

    def checkpoint_marker(self) -> int:
        """The step count recorded at the last boundary checkpoint, or 0."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT wake_count FROM checkpoint_marker WHERE id = 1"
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def record_checkpoint_marker(self, wake_count: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO checkpoint_marker (id, wake_count, recorded) "
                "VALUES (1, ?, ?) ON CONFLICT (id) DO UPDATE SET "
                "wake_count = excluded.wake_count, recorded = excluded.recorded",
                (int(wake_count), _iso(_now())),
            )

    def evaluation_marker(self) -> int:
        """The step count recorded at the last boundary evaluation, or 0."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT wake_count FROM evaluation_marker WHERE id = 1"
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def record_evaluation_marker(self, wake_count: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO evaluation_marker (id, wake_count, recorded) "
                "VALUES (1, ?, ?) ON CONFLICT (id) DO UPDATE SET "
                "wake_count = excluded.wake_count, recorded = excluded.recorded",
                (int(wake_count), _iso(_now())),
            )

    def assigned_repos(self, agent: str) -> tuple[str, ...]:
        """The repositories the last onboarding granted this agent, in order."""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT repo FROM agent_repos WHERE agent = ? ORDER BY rowid",
                (agent,),
            ).fetchall()
        return tuple(row["repo"] for row in rows)

    def record_assignment(self, agent: str, repos: tuple[str, ...]) -> None:
        """Replace the recorded grants for this agent with ``repos``."""

        with self._connect() as connection:
            connection.execute("DELETE FROM agent_repos WHERE agent = ?", (agent,))
            connection.executemany(
                "INSERT INTO agent_repos (agent, repo) VALUES (?, ?)",
                [(agent, repo) for repo in repos],
            )

    def run_wakes(self, agent: str) -> int:
        """Wakes one agent has started over the whole run, failed ones included.

        A wake that timed out or errored still happened, and the run budget
        is a cap on wakes taken, not on wakes that went well.
        """

        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS wakes FROM wakes WHERE agent = ?", (agent,)
            ).fetchone()
        return int(row["wakes"])

    def ledger_allocation(self) -> tuple[str | None, dict[str, int]]:
        """The budget file version last applied and its wakes per agent."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT sha, allocation FROM budget_ledger WHERE id = 1"
            ).fetchone()
        if row is None:
            return None, {}
        allocation = json.loads(row["allocation"])
        return str(row["sha"]), {str(k): int(v) for k, v in allocation.items()}

    def set_ledger_allocation(self, sha: str, allocation: dict[str, int]) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO budget_ledger (id, sha, allocation, applied) "
                "VALUES (1, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET "
                "sha = excluded.sha, allocation = excluded.allocation, "
                "applied = excluded.applied",
                (sha, json.dumps(allocation, sort_keys=True), _iso(_now())),
            )

    def mark_treatment(self, wake_id: int, treatment: str, detail: str = "") -> None:
        """Record that a wake ran under a treatment (a collaboration wake)."""

        with self._connect() as connection:
            connection.execute(
                "INSERT INTO wake_treatments (wake_id, treatment, detail) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT (wake_id) DO UPDATE SET treatment = excluded.treatment, "
                "detail = excluded.detail",
                (wake_id, treatment, detail),
            )

    def treatment(self, wake_id: int) -> tuple[str, str] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT treatment, detail FROM wake_treatments WHERE wake_id = ?",
                (wake_id,),
            ).fetchone()
        return (
            None if row is None else (str(row["treatment"]), str(row["detail"] or ""))
        )

    def day_usage(self, agent: str, day: str | None = None) -> tuple[int, float]:
        """Wakes started and cost accrued for one agent on one UTC day."""

        day = day or _now().strftime("%Y-%m-%d")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS wakes, COALESCE(SUM(cost_usd), 0) AS cost "
                "FROM wakes WHERE agent = ? AND day = ?",
                (agent, day),
            ).fetchone()
        return int(row["wakes"]), float(row["cost"])


def _due(now: datetime, seconds: int) -> datetime:
    return datetime.fromtimestamp(now.timestamp() + seconds, tz=UTC)
