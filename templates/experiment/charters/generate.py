#!/usr/bin/env python3
"""Generate neutral per-agent charters from one shared template.

This is one charter-design option, suited to experiments where maintainers
should discover opportunities and dependency relationships themselves. Shared
instructions remain identical; only repository identity, pin, and indispensable
factual scope may differ.
"""

from __future__ import annotations

from pathlib import Path
from string import Template

import yaml

# FILL: add only ownership facts needed to remove ambiguity, or delete this
# comment when no repository needs a scope clarification. Do not add suggested
# optimizations, named neighbours, expected outcomes, or evaluator details.
SCOPES: dict[str, str] = {
    # "monorepo": "Both `python-package/` and `native-core/` are in scope.\n",
}


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    world = yaml.safe_load((root / "world.yaml").read_text(encoding="utf-8"))
    template = Template(
        (Path(__file__).parent / "neutral-template.md").read_text(encoding="utf-8")
    )
    output = Path(__file__).parent
    expected: set[str] = set()

    for repository in world["repos"]:
        repo = repository["name"]
        pin = str(repository["pin"]["value"])
        filename = f"{repo}-bot.md"
        expected.add(filename)
        (output / filename).write_text(
            template.substitute(
                repo=repo,
                pin=pin,
                scope=SCOPES.get(repo, ""),
            ),
            encoding="utf-8",
        )

    for path in output.glob("*-bot.md"):
        if path.name not in expected:
            path.unlink()

    print(f"wrote {len(expected)} charters to {output}")


if __name__ == "__main__":
    main()
