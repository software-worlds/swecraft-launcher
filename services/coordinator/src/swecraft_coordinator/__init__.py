"""Autonomous per-repository maintainer loop over the one-shot runner."""

__all__ = ["CoordinatorError"]


class CoordinatorError(RuntimeError):
    """Raised when the coordinator cannot safely continue."""
