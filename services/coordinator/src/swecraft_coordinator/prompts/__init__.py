"""Render the coordinator's prompt templates with strict Jinja semantics."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from jinja2 import (
    ChoiceLoader,
    Environment,
    FileSystemLoader,
    PackageLoader,
    StrictUndefined,
    TemplateError,
)

from swecraft_coordinator import CoordinatorError

TEMPLATES = ("wake.md", "long_wake.md", "bootstrap.md")


def render(
    name: str, variables: Mapping[str, object], overrides: Path | None = None
) -> str:
    if name not in TEMPLATES:
        raise CoordinatorError(f"no prompt template named {name!r}")
    loaders = []
    if overrides is not None:
        loaders.append(FileSystemLoader(str(overrides)))
    loaders.append(PackageLoader("swecraft_coordinator", "prompts"))
    environment = Environment(
        loader=ChoiceLoader(loaders),
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
    )
    try:
        return environment.get_template(name).render(variables).strip() + "\n"
    except (OSError, TemplateError, UnicodeError) as error:
        raise CoordinatorError(f"prompt template rendering failed: {error}") from None


def render_wake(variables: Mapping[str, object], overrides: Path | None = None) -> str:
    return render("wake.md", variables, overrides)


def render_long_wake(
    variables: Mapping[str, object], overrides: Path | None = None
) -> str:
    """The long-wake prompt: the wake prompt's contract, kept going to a budget."""

    return render("long_wake.md", variables, overrides)
