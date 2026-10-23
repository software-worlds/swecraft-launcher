"""Forge runtime failures."""

from swecraft_common import HubError


class ForgeRuntimeError(HubError):
    """Raised when local Forge lifecycle or frozen state is invalid."""
