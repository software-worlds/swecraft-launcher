"""`swecraft-index-cap serve` runs the capped index; `show` inspects one project."""

from __future__ import annotations

import argparse
import logging
import sys

from swecraft_index_cap.index import IndexCapError, cap_page, parse_cutoff
from swecraft_index_cap.server import UPSTREAM, Upstream, UpstreamError, serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="swecraft-index-cap",
        description="PyPI's simple index as it stood at a cutoff, for seeded worlds.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("serve", help="serve /<cutoff>/simple/<project>/")
    # Every interface by default: agent containers reach the host on the Docker
    # bridge, CI jobs inside a forge's Docker-in-Docker only on the host's own
    # address, and one listener has to answer both.
    run.add_argument("--host", default="0.0.0.0")
    run.add_argument("--port", type=int, default=5280)
    run.add_argument("--upstream", default=UPSTREAM, help="the simple index to cap")
    run.add_argument("--verbose", action="store_true", help="log every request line")
    show = commands.add_parser("show", help="print what a cutoff leaves of a project")
    show.add_argument("cutoff", help="a date or an RFC 3339 timestamp")
    show.add_argument("project")
    show.add_argument("--upstream", default=UPSTREAM)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    logging.Formatter.converter = __import__("time").gmtime
    if args.command == "serve":
        serve(args.host, args.port, upstream=Upstream(args.upstream))
        return 0
    try:
        cutoff = parse_cutoff(args.cutoff)
        page = Upstream(args.upstream).page(args.project)
        if page is None:
            print(f"no project {args.project}", file=sys.stderr)
            return 1
        capped = cap_page(page, cutoff)
    except (IndexCapError, UpstreamError) as error:
        print(str(error), file=sys.stderr)
        return 1
    versions = capped.page.get("versions") or []
    print(
        f"{args.project} before {cutoff.isoformat()}: {capped.kept} files kept, "
        f"{capped.dropped} withheld; newest version {versions[-1] if versions else '-'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
