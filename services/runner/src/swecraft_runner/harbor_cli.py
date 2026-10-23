"""Harbor's CLI with the in-process switch applied: the ``swecraft-harbor`` script.

The runner drives Harbor as a subprocess, so anything that has to happen
inside Harbor's process (a change to a Harbor class) cannot be done by the
runner itself. This module is what the runner's default Harbor command runs:
it applies the switch below and then hands the command line to Harbor's own
CLI, so ``--version`` and every subcommand behave exactly as stock Harbor. A
site launcher named through ``harbor_command`` replaces this entry point and
is then responsible for the same: call :func:`apply_process_fixes` before
delegating to ``harbor.cli.main.app``. ``swecraft_runner.harbor_agents`` applies
it on import as well, so an agent class loaded by import path gets it under
any launcher.

**The scrub switch.** Since 0.20.0 Harbor scrubs the trial directory when a
trial ends (``Trial._scrub_jobs_dir``, unconditionally, in the trial's
``finally``): it takes the value of every declared environment variable
whose *name* looks sensitive (``KEY``, ``SECRET``, ``TOKEN``, ``PASSWORD``,
``CREDENTIAL``, ``AUTH``) and replaces every occurrence in every text file
under the trial directory, collected artifacts included, with
``[REDACTED]`` -- with no regard for what the value is. A flag such as
``AGY_ADC_AUTH=true`` or ``CLAUDE_FORCE_OAUTH=1`` therefore erases every
``true`` or ``1`` in ``result.json`` and the trajectory, and the runner then
fails the run on invalid JSON; a direct-action wake passes git's numbered
configuration pairs, and ``GIT_CONFIG_KEY_<n>`` (``user.name``,
``user.email``, ``http.<forge>/.extraheader``) is scrubbed out of the
collected repository, which the runner then commits as the agent's work.
Harbor offers no way to turn the pass off, so this wrapper does: it is **off**
by default and ``SWECRAFT_HARBOR_SCRUB=on`` restores the stock behaviour. With
the pass off, the runner's own redaction of receipts and captured output
(values of eight characters or more, see ``MIN_REDACTABLE_SECRET_LENGTH``
in :mod:`swecraft_runner.runner`) still applies; only Harbor's extra pass over
collected artifacts is lost.
"""

from __future__ import annotations

import functools
import os
from collections.abc import MutableMapping
from typing import Any

# The switch, read from the environment Harbor's process inherits (the
# coordinator host's, or a site launcher's). Anything but these two values
# fails the launcher at start, so a typo cannot silently pick a setting.
SCRUB_ENV = "SWECRAFT_HARBOR_SCRUB"
SCRUB_SETTINGS = ("off", "on")
DEFAULT_SCRUB_SETTING = "off"

# Set on Harbor's Trial class once the switch has been applied.
_SCRUB_MARK = "_hub_scrub_switch"


def scrub_setting(environ: MutableMapping[str, str] | None = None) -> str:
    """``off`` or ``on``, from :data:`SCRUB_ENV`; ``off`` when unset or blank.

    Raises ``ValueError`` for any other value; the launcher lets that
    propagate, so a misspelt setting stops the run instead of running under
    a setting nobody chose.
    """

    source = os.environ if environ is None else environ
    raw = source.get(SCRUB_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_SCRUB_SETTING
    setting = raw.strip().lower()
    if setting not in SCRUB_SETTINGS:
        raise ValueError(
            f"{SCRUB_ENV}={raw!r}: expected one of {', '.join(SCRUB_SETTINGS)}"
        )
    return setting


def apply_scrub_switch(
    trial_class: type | None = None, setting: str | None = None
) -> bool:
    """Turn Harbor's end-of-trial scrub off unless the switch says ``on``.

    Returns whether this call changed the class; ``False`` means the
    setting is ``on`` (stock behaviour kept) or the switch was already
    applied. ``trial_class`` defaults to Harbor's own ``Trial``; it is a
    parameter so the behaviour can be exercised without Harbor installed.
    """

    if setting is None:
        setting = scrub_setting()
    if setting == "on":
        return False
    if trial_class is None:
        from harbor.trial.trial import Trial

        trial_class = Trial
    if getattr(trial_class, _SCRUB_MARK, False):
        return False
    original = trial_class._scrub_jobs_dir

    @functools.wraps(original)
    def scrub_off(self: Any) -> None:
        logger = getattr(self, "logger", None)
        if logger is not None:
            logger.info(
                "Harbor's trial-directory scrub is off (%s=on restores it)",
                SCRUB_ENV,
            )

    trial_class._scrub_jobs_dir = scrub_off
    setattr(trial_class, _SCRUB_MARK, True)
    return True


def apply_process_fixes() -> None:
    """Apply every in-process change; safe to call more than once."""

    apply_scrub_switch()


def main() -> Any:
    """Run Harbor's CLI with the switch applied (the ``swecraft-harbor`` script)."""

    apply_process_fixes()
    from harbor.cli.main import app

    return app()
