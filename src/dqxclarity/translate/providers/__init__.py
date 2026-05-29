"""Pluggable translation providers (MT fallback for text not in the curated DB)."""

from __future__ import annotations

from .base import Provider


def get_provider(name: str, **kwargs) -> Provider | None:
    """Return a provider instance, or None for 'none'/unknown (pure-local mode)."""
    if name in ("", "none"):
        return None
    if name == "claude_cli":
        from .claude_cli import ClaudeCliProvider

        return ClaudeCliProvider(**kwargs)
    if name in ("googletranslatefree", "google"):
        from .googletranslatefree import GoogleTranslateFreeProvider

        return GoogleTranslateFreeProvider(**kwargs)
    raise ValueError(f"unknown translation provider: {name!r}")
