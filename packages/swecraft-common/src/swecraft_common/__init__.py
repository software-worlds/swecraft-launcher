"""Small host-Git, Forgejo, and run-ID primitives shared by Hub."""

__all__ = ["HubError"]


class HubError(RuntimeError):
    """Raised when a SWECraft service cannot safely continue."""
