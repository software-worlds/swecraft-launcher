"""Run one experiment end to end, from an experiment folder.

    swecraft-launch <command> experiments/<name>
    just exp <command> experiments/<name>       # the same, through uv

The commands, in the order a run uses them:

  check     the folder is complete: the four manifests, a charter per agent,
            no FILL marker, and settings that agree with each other
  up        the capped package index (when SWECRAFT_FORGE_INDEX_URL is set) and the
            forge, with CI limits
  seed      seed the world, prove the seed queued no CI run, enable CI, and
            check the forge against the lock
  shakeout  mint the evaluator's read token, validate the eval set, and run an
            A/A sweep on the untouched seed; every gate must pass
  onboard   onboard every agent with its charter, then one bootstrap turn each
            (model spending starts here)
  run       serve the coordinator and sweep the world on a timer until the
            stop rule is met, then drain and finish; Ctrl-C drains and pauses
  finish    final sweep, score, and one verified checkpoint with an archive
  status    the wake ledger, spend, the stop rule, and the newest sweep's gates
  export    collect a checkpoint and export it as a browsable bundle

Settings come from <experiment>/experiment.env (KEY=VALUE lines; see
templates/experiment/experiment.env). Runtime state lives under the checkout's
.swecraft/ and the forge's state under services/forge/state, so one checkout runs
one experiment at a time.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import yaml


def checkout() -> Path:
    """The checkout this command runs in: the nearest folder holding the forge's
    compose file, up from the working directory, else the one this module is in."""

    for folder in (Path.cwd(), *Path.cwd().parents, *Path(__file__).resolve().parents):
        if (folder / "services/forge/compose.yaml").is_file():
            return folder
    return Path.cwd()


ROOT = checkout()
HUB = ROOT / ".swecraft"
COMPOSE = ROOT / "services/forge/compose.yaml"
FILES = ("world.yaml", "evaluation.yaml", "coordinator.yaml", "checkpoint.yaml")
DEFAULTS = {
    "SWECRAFT_FORGE_HOST": "127.0.0.1",
    "SWECRAFT_FORGE_PORT": "3400",
    "COMPOSE_PROJECT_NAME": "swecraft-forge",
    "EVALUATION": "",
    "MEASURE_INTERVAL_SECONDS": "7200",
    "STOP_WAKES": "",
    "STOP_DOLLARS": "",
    "STOP_AGENTS": "",
    "BOOTSTRAP_PARALLEL": "6",
    "FIX_OWNERSHIP": "1",
    "ARCHIVE_DIR": "archives",
}


class Failed(Exception):
    """A step failed; the message says which and what to do."""


def say(message: str) -> None:
    print(f"{datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ} {message}", flush=True)


class Experiment:
    def __init__(self, folder: Path) -> None:
        self.dir = folder.resolve()
        if not self.dir.is_dir():
            raise Failed(f"no experiment folder at {folder}")
        try:
            self.rel = self.dir.relative_to(ROOT)
        except ValueError:
            raise Failed(f"the experiment folder must sit inside {ROOT}") from None
        self.settings = dict(DEFAULTS)
        env_file = self.dir / "experiment.env"
        if env_file.is_file():
            self.settings.update(read_env(env_file))
        self.coordinator = self._yaml("coordinator.yaml")
        self.agents = [str(a["name"]) for a in self.coordinator.get("agents") or []]
        self.evaluation = self.settings["EVALUATION"] or self._only_evaluation()

    def _yaml(self, name: str) -> dict:
        path = self.dir / name
        if not path.is_file():
            return {}
        return yaml.safe_load(path.read_text()) or {}

    def _only_evaluation(self) -> str:
        names = list((self._yaml("evaluation.yaml").get("evaluations") or {}).keys())
        return names[0] if len(names) == 1 else ""

    @property
    def forge_url(self) -> str:
        s = self.settings
        return f"http://{s['SWECRAFT_FORGE_HOST']}:{s['SWECRAFT_FORGE_PORT']}"

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update({k: v for k, v in self.settings.items() if v != ""})
        return env

    def run(
        self, argv: list[str], *, check: bool = True, **kw
    ) -> subprocess.CompletedProcess:
        say("$ " + shlex.join(argv))
        done = subprocess.run(argv, cwd=ROOT, env=self.env(), **kw)
        if check and done.returncode != 0:
            raise Failed(f"exit {done.returncode}: {shlex.join(argv)}")
        return done

    # The workspace's own entry points, so a signal reaches the Python process.
    def tool(self, name: str) -> str:
        found = shutil.which(name)
        if not found:
            raise Failed(
                f"{name} is not installed; run this through `uv run` "
                "(or `just exp`) in the checkout"
            )
        return found

    def world(self, *args: str) -> list[str]:
        return [
            self.tool("swecraft-world"),
            "--manifest",
            str(self.rel / "world.yaml"),
            *args,
        ]

    def evaluator(self, *args: str) -> list[str]:
        return [
            self.tool("swecraft-evaluator"),
            "--manifest",
            str(self.rel / "evaluation.yaml"),
            "--forge-url",
            self.forge_url,
            "--forge-token-file",
            ".swecraft/evaluator_token",
            *args,
        ]

    def coordinator_cli(self, *args: str) -> list[str]:
        return [
            self.tool("swecraft-coordinator"),
            "--config",
            str(self.rel / "coordinator.yaml"),
            *args,
        ]


def read_env(path: Path) -> dict[str, str]:
    out = {}
    for number, line in enumerate(path.read_text().splitlines(), 1):
        line = (
            line.split("#", 1)[0].strip() if not line.lstrip().startswith("#") else ""
        )
        if not line:
            continue
        key, sep, value = line.partition("=")
        if not sep or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key.strip()):
            raise Failed(f"{path}:{number}: expected KEY=VALUE")
        out[key.strip()] = value.strip().strip("'\"")
    return out


def pid_alive(pidfile: Path) -> int | None:
    try:
        pid = int(pidfile.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def index_base(url: str) -> str:
    match = re.match(r"^(https?://[^/]+)", url)
    if not match:
        raise Failed(f"SWECRAFT_FORGE_INDEX_URL is not a URL: {url}")
    return match.group(1)


def answers(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status == 200
    except OSError:
        return False


# check --------------------------------------------------------------------


def check(x: Experiment) -> list[str]:
    problems = []
    for name in FILES:
        if not (x.dir / name).is_file():
            problems.append(f"missing {name}")
    for agent in x.agents:
        if not (x.dir / "charters" / f"{agent}.md").is_file():
            problems.append(f"agent {agent} has no charters/{agent}.md")
    if not x.agents:
        problems.append("coordinator.yaml lists no agents")
    for path in sorted(x.dir.rglob("*")):
        if path.is_file() and path.suffix in {
            ".yaml",
            ".yml",
            ".md",
            ".env",
            ".py",
            "",
        }:
            text = path.read_text(errors="replace")
            if re.search(r"\bFILL\b", text):
                problems.append(f"FILL marker left in {path.relative_to(x.dir)}")
    forge_url = str(x.coordinator.get("forge_url") or "")
    if forge_url and not forge_url.endswith(f":{x.settings['SWECRAFT_FORGE_PORT']}"):
        problems.append(
            f"coordinator.yaml forge_url {forge_url} is not on "
            f"SWECRAFT_FORGE_PORT {x.settings['SWECRAFT_FORGE_PORT']}"
        )
    checkpoint = x._yaml("checkpoint.yaml")
    root = str((checkpoint.get("apparatus") or {}).get("root") or "")
    if root and Path(root) != x.rel:
        problems.append(f"checkpoint.yaml apparatus.root is {root}, not {x.rel}")
    project = str((checkpoint.get("checkpoint") or {}).get("compose_project") or "")
    if project and project != x.settings["COMPOSE_PROJECT_NAME"]:
        problems.append(
            f"checkpoint.yaml compose_project {project} is not COMPOSE_PROJECT_NAME"
        )
    expected = str(x.rel / "checkpoint.yaml")
    if str(x.coordinator.get("checkpoint_config") or expected) != expected:
        problems.append(f"coordinator.yaml checkpoint_config should be {expected}")
    if not x.evaluation:
        problems.append(
            "set EVALUATION in experiment.env (evaluation.yaml names more than one)"
        )
    for key in ("STOP_WAKES", "STOP_DOLLARS"):
        if x.settings[key] and number(x.settings[key]) is None:
            problems.append(f"{key} in experiment.env is not a number")
    if not (number(x.settings["STOP_WAKES"]) or number(x.settings["STOP_DOLLARS"])):
        problems.append(
            "no stop rule: set STOP_WAKES and/or STOP_DOLLARS in experiment.env"
        )
    return problems


def cmd_check(x: Experiment, _: argparse.Namespace) -> None:
    problems = check(x)
    for problem in problems:
        print(f"  - {problem}")
    if problems:
        raise Failed(f"{len(problems)} problem(s) in {x.rel}")
    say(f"{x.rel}: complete ({len(x.agents)} agents, evaluation {x.evaluation})")


# up / seed / shakeout / onboard -------------------------------------------


def ensure_index(x: Experiment) -> None:
    url = x.settings.get("SWECRAFT_FORGE_INDEX_URL", "")
    if not url:
        return
    health = index_base(url) + "/healthz"
    if answers(health):
        return
    port = re.search(r":(\d+)$", index_base(url))
    HUB.mkdir(exist_ok=True)
    say(f"the capped index does not answer at {health}; starting swecraft-index-cap")
    log = open(HUB / "index-cap.log", "ab")
    process = subprocess.Popen(
        [
            x.tool("swecraft-index-cap"),
            "serve",
            "--host",
            "0.0.0.0",
            "--port",
            port.group(1) if port else "5280",
        ],
        cwd=ROOT,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    (HUB / "index-cap.pid").write_text(f"{process.pid}\n")
    for _ in range(30):
        if answers(health):
            return
        time.sleep(1)
    raise Failed(
        f"the capped index never answered at {health} (see .swecraft/index-cap.log)"
    )


def cmd_up(x: Experiment, _: argparse.Namespace) -> None:
    ensure_index(x)
    x.run(["just", "forge-up"])
    x.run([str(ROOT / "services/forge/bootstrap.sh"), "--no-repositories"])
    runner = ROOT / "services/forge/state/runner/config.yml"
    if runner.is_file():
        text = runner.read_text()
        if "options:" not in text:
            raise Failed(
                f"CI job limits missing from {runner}; remove it and run `up` again"
            )
        if x.settings.get("SWECRAFT_FORGE_INDEX_URL") and "PIP_INDEX_URL" not in text:
            raise Failed(
                f"CI index settings missing from {runner}; remove it and run `up` again"
            )
    say(f"forge up at {x.forge_url}")


def cmd_seed(x: Experiment, _: argparse.Namespace) -> None:
    x.run(x.world("seed"))
    status = x.run(x.world("actions-status"), capture_output=True, text=True)
    print(status.stdout, end="")
    last = status.stdout.strip().splitlines()[-1] if status.stdout.strip() else ""
    if not last.endswith(": 0"):
        raise Failed(
            "the seed queued CI runs: the world is contaminated; "
            "reset the forge and seed again"
        )
    x.run(x.world("enable-actions"))
    x.run(x.world("actions-status"))
    x.run(x.world("check"))
    say(f"seeded; commit {x.rel / 'world.lock.json'} with the experiment")


def mint_evaluator_token(x: Experiment) -> None:
    token = HUB / "evaluator_token"
    if token.is_file() and token.stat().st_size:
        return
    HUB.mkdir(exist_ok=True)
    say(
        "minting the evaluator's read-scoped forge token into .swecraft/evaluator_token"
    )
    argv = [
        "docker",
        "compose",
        "-p",
        x.settings["COMPOSE_PROJECT_NAME"],
        "-f",
        str(COMPOSE),
        "exec",
        "-T",
        "forgejo",
        "forgejo",
        "admin",
        "user",
        "generate-access-token",
        "--username",
        x.settings.get("SWECRAFT_FORGE_ADMIN") or "swecraft-admin",
        "--token-name",
        f"evaluator-read-{int(time.time())}",
        "--scopes",
        "read:repository,read:organization,read:user",
        "--raw",
    ]
    done = subprocess.run(argv, cwd=ROOT, env=x.env(), capture_output=True, text=True)
    value = done.stdout.strip()
    if done.returncode != 0 or not value:
        raise Failed("could not mint the evaluator token (is the forge up?)")
    old = os.umask(0o077)
    try:
        token.write_text(value + "\n")
    finally:
        os.umask(old)
    token.chmod(0o600)


def newest_record(x: Experiment) -> dict | None:
    records = sorted((HUB / "evaluator/records").glob(f"*{x.evaluation}*"))
    for folder in reversed(records):
        path = folder / "record.json"
        if path.is_file():
            return json.loads(path.read_text())
    return None


def gates(record: dict) -> tuple[int, int]:
    evaluators = record.get("evaluators") or {}
    return sum(1 for v in evaluators.values() if v.get("gate")), len(evaluators)


def cmd_shakeout(x: Experiment, _: argparse.Namespace) -> None:
    mint_evaluator_token(x)
    x.run(x.evaluator("validate"))
    x.run(x.evaluator("run", x.evaluation, "--env", "world@latest"))
    record = newest_record(x)
    if record is None:
        raise Failed("the sweep wrote no record")
    passed, total = gates(record)
    x.run(x.evaluator("score", x.evaluation), check=False)
    if passed != total:
        raise Failed(
            f"A/A gates red: {passed}/{total} passed; "
            "fix the eval set before onboarding"
        )
    say(f"A/A green: {passed}/{total} gates")


def cmd_onboard(x: Experiment, _: argparse.Namespace) -> None:
    ensure_index(x)
    for agent in x.agents:
        x.run(
            x.coordinator_cli(
                "onboard", agent, "--goal-file", str(x.rel / "charters" / f"{agent}.md")
            )
        )
    say(f"bootstrapping {len(x.agents)} agent(s); spending starts")
    logs = HUB / "bootstrap"
    logs.mkdir(parents=True, exist_ok=True)

    def bootstrap(agent: str) -> tuple[str, int]:
        with open(logs / f"{agent}.log", "ab") as log:
            done = subprocess.run(
                x.coordinator_cli("bootstrap", agent),
                cwd=ROOT,
                env=x.env(),
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        return agent, done.returncode

    with ThreadPoolExecutor(max_workers=int(x.settings["BOOTSTRAP_PARALLEL"])) as pool:
        results = list(pool.map(bootstrap, x.agents))
    failed = [agent for agent, code in results if code != 0]
    for agent, code in results:
        say(
            f"  {'ok  ' if code == 0 else 'FAIL'} {agent} "
            f"(.swecraft/bootstrap/{agent}.log)"
        )
    if failed:
        raise Failed(
            f"bootstrap failed for {', '.join(failed)}; "
            "rerun `onboard` (onboarding is idempotent)"
        )


# run / finish -------------------------------------------------------------


def ledger(x: Experiment) -> dict:
    database = ROOT / str(x.coordinator.get("database") or ".swecraft/coordinator.db")
    if not database.is_file():
        return {"completed": 0, "failed": 0, "cost_usd": 0.0}
    agents = [a.strip() for a in x.settings["STOP_AGENTS"].split(",") if a.strip()]
    where = f"agent IN ({','.join('?' * len(agents))})" if agents else "1=1"
    db = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        counts = dict(
            db.execute(
                f"SELECT status, COUNT(*) FROM wakes WHERE {where} GROUP BY status",
                agents,
            )
        )
        cost = db.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM wakes").fetchone()[0]
    finally:
        db.close()
    return {
        "completed": counts.get("completed", 0),
        "failed": counts.get("failed", 0),
        "cost_usd": float(cost),
    }


def number(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


def stop_reached(x: Experiment, now: dict) -> str | None:
    wakes, dollars = (
        number(x.settings["STOP_WAKES"]),
        number(x.settings["STOP_DOLLARS"]),
    )
    if wakes is not None and now["completed"] >= wakes:
        return f"{now['completed']} completed wakes >= STOP_WAKES {wakes:g}"
    if dollars is not None and now["cost_usd"] >= dollars:
        return f"${now['cost_usd']:.2f} spent >= STOP_DOLLARS {dollars:g}"
    return None


def fix_ownership(x: Experiment) -> None:
    """Rootful Docker leaves container-written files root-owned under .swecraft/;
    retention, the receipt scrub and checkpoints then trip on them. Evaluator
    records are touched only once they are two hours old, never mid-sweep."""

    if (
        x.settings["FIX_OWNERSHIP"] in ("0", "")
        or os.geteuid() == 0
        or not shutil.which("docker")
    ):
        return
    owner = f"{os.getuid()}:{os.getgid()}"
    for folder, age in ((HUB / "runs", ""), (HUB / "evaluator/records", "-mmin +120")):
        if folder.is_dir():
            subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "-v",
                    f"{folder}:/x",
                    "alpine",
                    "sh",
                    "-c",
                    f"find /x {age} ! -user {os.getuid()} -exec chown {owner} {{}} +",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


class Sweeps(threading.Thread):
    """world@latest every MEASURE_INTERVAL_SECONDS, the first one at once."""

    def __init__(self, x: Experiment) -> None:
        super().__init__(name="sweeps", daemon=True)
        self.x, self.stopping = x, threading.Event()
        self.interval = int(x.settings["MEASURE_INTERVAL_SECONDS"])

    def run(self) -> None:
        number = 0
        while not self.stopping.is_set():
            number += 1
            say(f"sweep {number}")
            with open(HUB / "measure-loop.log", "ab") as log:
                process = subprocess.Popen(
                    self.x.evaluator("run", self.x.evaluation, "--env", "world@latest"),
                    cwd=ROOT,
                    env=self.x.env(),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                (HUB / "sweep.pid").write_text(f"{process.pid}\n")
                code = process.wait()
            (HUB / "sweep.pid").unlink(missing_ok=True)
            say(f"sweep {number} exit {code}")
            self.stopping.wait(self.interval)

    def stop(self) -> None:
        self.stopping.set()
        if self.is_alive() and (HUB / "sweep.pid").exists():
            say("waiting for the sweep in flight")
        self.join()


def cmd_run(x: Experiment, args: argparse.Namespace) -> None:
    problems = check(x)
    if problems:
        raise Failed("not ready: " + "; ".join(problems))
    if not (HUB / "evaluator_token").is_file():
        raise Failed("no evaluator token: run `shakeout` first")
    if (HUB / "FINISHED").exists():
        raise Failed(".swecraft/FINISHED exists: this experiment was finished")
    if pid := pid_alive(HUB / "coordinator.pid"):
        raise Failed(f"a coordinator is already running (pid {pid})")
    ensure_index(x)
    HUB.mkdir(exist_ok=True)
    signals = []
    for number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(number, lambda n, _f: signals.append(n))

    log = open(HUB / "coordinator-serve.log", "ab")
    coordinator = subprocess.Popen(
        x.coordinator_cli("serve"),
        cwd=ROOT,
        env=x.env(),
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    (HUB / "coordinator.pid").write_text(f"{coordinator.pid}\n")
    say(
        f"coordinator serving (pid {coordinator.pid}, "
        "log .swecraft/coordinator-serve.log)"
    )
    sweeps = Sweeps(x)
    sweeps.start()
    rule = (x.settings["STOP_WAKES"] or "-", x.settings["STOP_DOLLARS"] or "-")
    say(f"stop rule: {rule[0]} completed wakes or ${rule[1]} spent")

    reason, last_check, last_fix = None, 0.0, time.monotonic()
    while reason is None:
        time.sleep(1)
        if signals:
            reason = "interrupted"
        elif coordinator.poll() is not None:
            reason = f"coordinator exited ({coordinator.returncode})"
        elif time.monotonic() - last_check >= 30:
            last_check = time.monotonic()
            reason = stop_reached(x, ledger(x))
            if reason is None and time.monotonic() - last_fix >= 300:
                fix_ownership(x)
                last_fix = time.monotonic()

    say(f"stopping: {reason}")
    if coordinator.poll() is None:
        say(
            f"draining the coordinator (one SIGINT to pid {coordinator.pid}); "
            "in-flight wakes finish"
        )
        coordinator.send_signal(signal.SIGINT)
        coordinator.wait()
    (HUB / "coordinator.pid").unlink(missing_ok=True)
    sweeps.stop()
    fix_ownership(x)
    if reason.startswith("coordinator exited"):
        raise Failed(
            f"{reason}: see .swecraft/coordinator-serve.log; `run` again to continue"
        )
    if reason == "interrupted":
        say("paused: `run` continues the experiment, `finish` closes it")
        return
    cmd_finish(x, args)


def cmd_finish(x: Experiment, _: argparse.Namespace) -> None:
    if pid := pid_alive(HUB / "coordinator.pid"):
        raise Failed(f"a coordinator is still running (pid {pid}); stop `run` first")
    if pid := pid_alive(HUB / "sweep.pid"):
        raise Failed(f"a sweep is still running (pid {pid})")
    fix_ownership(x)
    say("final sweep")
    x.run(x.evaluator("run", x.evaluation, "--env", "world@latest"), check=False)
    x.run(x.evaluator("score", x.evaluation), check=False)
    folder = ROOT / x.settings["ARCHIVE_DIR"]
    folder.mkdir(parents=True, exist_ok=True)
    archive = folder / f"{x.dir.name}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.tar.gz"
    x.run(
        [
            x.tool("swecraft-society"),
            "checkpoint",
            "--config",
            str(x.rel / "checkpoint.yaml"),
            "--archive",
            str(archive),
        ]
    )
    (HUB / "FINISHED").write_text(datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ\n"))
    say(f"finished; newest checkpoint in .swecraft/checkpoints, archive {archive}")


# status / export ------------------------------------------------------------


def cmd_status(x: Experiment, _: argparse.Namespace) -> None:
    now = ledger(x)
    pid = pid_alive(HUB / "coordinator.pid")
    print(f"experiment   {x.rel}")
    print(f"coordinator  {f'running (pid {pid})' if pid else 'not running'}")
    print(
        f"wakes        {now['completed']} completed, {now['failed']} failed, "
        f"${now['cost_usd']:.2f}"
    )
    print(
        f"stop rule    {x.settings['STOP_WAKES'] or '-'} wakes / "
        f"${x.settings['STOP_DOLLARS'] or '-'}"
        f"{'  (reached: ' + r + ')' if (r := stop_reached(x, now)) else ''}"
    )
    record = newest_record(x)
    if record:
        passed, total = gates(record)
        print(f"newest sweep gates {passed}/{total}")
    print(f"finished     {'yes' if (HUB / 'FINISHED').exists() else 'no'}")


def hex_spiral(count: int) -> list[list[int]]:
    """Axial hex coordinates, ring by ring around the origin: a default map."""

    cells, radius = [[0, 0]], 1
    steps = [(1, 0), (1, -1), (0, -1), (-1, 0), (-1, 1), (0, 1)]
    while len(cells) < count:
        q, r = -radius, radius
        for dq, dr in steps:
            for _ in range(radius):
                cells.append([q, r])
                q, r = q + dq, r + dr
        radius += 1
    return cells[:count]


def default_layout(x: Experiment, out: Path) -> Path:
    world = x._yaml("world.yaml")
    repos = world.get("repos") or []
    layout = {
        "world": {
            "id": str(world.get("world_id") or x.dir.name),
            "label": str(world.get("world_id") or x.dir.name),
            "org": str(world.get("org") or "swecraft"),
            "repos": [
                {
                    "name": str(repo["name"]),
                    "upstream": repo.get("upstream"),
                    "hex": cell,
                    "languages": {},
                }
                for repo, cell in zip(repos, hex_spiral(len(repos)), strict=True)
            ],
            "deps": [],
        }
    }
    out.write_text(json.dumps(layout, indent=2) + "\n")
    return out


def cmd_export(x: Experiment, args: argparse.Namespace) -> None:
    if args.checkpoint:
        checkpoint = Path(args.checkpoint).resolve()
    else:
        found = sorted(
            p
            for p in (HUB / "checkpoints").glob("*")
            if (p / "manifest.json").is_file()
        )
        if not found:
            raise Failed(
                "no checkpoint under .swecraft/checkpoints; "
                "run `finish` or pass --checkpoint"
            )
        checkpoint = found[-1]
    analysis = HUB / "analysis" / checkpoint.name
    x.run([x.tool("swecraft-analyzer"), "collect", str(checkpoint), str(analysis)])
    layout = (
        Path(args.world).resolve()
        if args.world
        else default_layout(x, analysis / "layout.json")
    )
    out = Path(args.out).resolve()
    argv = [
        sys.executable,
        str(ROOT / "services/export/export_bundle.py"),
        "--evidence",
        str(analysis),
        "--checkpoint",
        str(checkpoint),
        "--world",
        str(layout),
        "--id",
        args.id or x.dir.name,
        "--label",
        args.label or x.dir.name,
        "--out",
        str(out),
    ]
    if args.known_secrets:
        argv += ["--known-secrets", str(Path(args.known_secrets).resolve())]
    x.run(argv)
    run_id = args.id or x.dir.name
    # The evaluation page: the evaluator's readings over the run's sessions. A run
    # without a ledger or records has none, and the bundle is complete without it.
    done = x.run(
        [
            sys.executable,
            str(ROOT / "services/export/evaluation.py"),
            "--checkpoint",
            str(checkpoint),
            "--id",
            run_id,
            "--out",
            str(out / run_id / "evaluation.json"),
        ],
        check=False,
    )
    if done.returncode != 0:
        say("no evaluation page: the checkpoint has no readable ledger or records")
    say(f"bundle written to {out / run_id}; serve {out} with sim-browser to browse it")


COMMANDS = {
    "check": cmd_check,
    "up": cmd_up,
    "seed": cmd_seed,
    "shakeout": cmd_shakeout,
    "onboard": cmd_onboard,
    "run": cmd_run,
    "finish": cmd_finish,
    "status": cmd_status,
    "export": cmd_export,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument(
        "experiment", type=Path, help="the experiment folder, e.g. experiments/demo"
    )
    parser.add_argument(
        "--checkpoint", help="export: this checkpoint instead of the newest"
    )
    parser.add_argument(
        "--world",
        help="export: a map layout for the browser (default: from world.yaml)",
    )
    parser.add_argument(
        "--known-secrets",
        help="export: a file of secret values to redact, one per line",
    )
    parser.add_argument(
        "--id",
        help="export: the run's id in the bundles folder (default: the folder name)",
    )
    parser.add_argument("--label", help="export: the run's name in lists")
    parser.add_argument(
        "--out", default="bundles", help="export: the bundles folder (default: bundles)"
    )
    args = parser.parse_args(argv)
    try:
        COMMANDS[args.command](Experiment(args.experiment), args)
    except Failed as error:
        say(f"FAILED: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
