"""CLI: collect a checkpoint into an evidence store, and query it.

Standard library only (argparse), so the same commands run anywhere Python
does, including inside a container without this workspace's packages.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from swecraft_analyzer import store
from swecraft_analyzer.checkpoint import AnalyzerError


def _collect(args: argparse.Namespace) -> int:
    from swecraft_analyzer.collect import collect

    summary = collect(
        args.checkpoint, args.out, forge=not args.no_forge, runs_dir=args.runs_dir
    )
    print(json.dumps({"counts": summary["counts"], "problems": summary["problems"]}))
    return 0


def _query(args: argparse.Namespace) -> int:
    """Read-only SQL over evidence.db, for containers without a sqlite3 CLI."""

    db = store.open_store(args.analysis / "evidence.db")
    try:
        try:
            cursor = db.execute(args.sql)
        except sqlite3.Error as error:
            raise AnalyzerError(f"query failed: {error}") from None
        columns = [column[0] for column in cursor.description or ()]
        for index, row in enumerate(cursor):
            if args.limit and index >= args.limit:
                print(f"... (stopped at --limit {args.limit})", file=sys.stderr)
                break
            if args.json:
                record = dict(zip(columns, row, strict=True))
                print(json.dumps(record, ensure_ascii=False))
            else:
                if index == 0:
                    print("\t".join(columns))
                print("\t".join("" if value is None else str(value) for value in row))
    finally:
        db.close()
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="swecraft-analyzer", description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)

    command = commands.add_parser("collect", help="build evidence.db from a checkpoint")
    command.add_argument("checkpoint", type=Path)
    command.add_argument("out", type=Path)
    command.add_argument("--no-forge", action="store_true")
    command.add_argument(
        "--runs-dir",
        type=Path,
        help="the live .swecraft/runs, for runs the checkpoint kept no transcript of",
    )
    command.set_defaults(handler=_collect)

    command = commands.add_parser("query", help="read-only SQL over evidence.db")
    command.add_argument("analysis", type=Path)
    command.add_argument("sql")
    command.add_argument("--json", action="store_true", help="one JSON object a row")
    command.add_argument("--limit", type=int, default=0)
    command.set_defaults(handler=_query)

    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.handler(args)
    except AnalyzerError as error:
        print(f"swecraft-analyzer: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
