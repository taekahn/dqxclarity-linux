"""Pluggable translation providers (MT fallback for text not in the curated DB)."""

from __future__ import annotations

import os
import shutil
import warnings

from .base import Provider


def get_provider(name: str, **kwargs) -> Provider | None:
    """Return a provider instance, or None for 'none'/unknown (pure-local mode).

    ``"claude"`` is the auto-resolving alias. When BOTH an ``ANTHROPIC_API_KEY`` and the ``claude``
    CLI are available it returns a composite that uses the metered HTTP API and falls back to the
    subscription CLI per item on any API miss (rate-limit/5xx/network/dropped item). With only one
    available it returns that provider directly; with neither it returns None (plus a non-fatal
    warning) so a misconfigured upgrade lane just disables the background upgrade rather than crashing.
    """
    if name in ("", "none"):
        return None
    if name == "claude":
        has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
        has_cli = bool(shutil.which("claude"))
        if has_key and has_cli:
            from ._claude_fallback import ClaudeFallbackProvider
            from .claude_api import ClaudeApiProvider
            from .claude_cli import ClaudeCliProvider

            return ClaudeFallbackProvider(
                ClaudeApiProvider(**kwargs), ClaudeCliProvider(**kwargs)
            )
        if has_key:
            from .claude_api import ClaudeApiProvider

            return ClaudeApiProvider(**kwargs)
        if has_cli:
            from .claude_cli import ClaudeCliProvider

            return ClaudeCliProvider(**kwargs)
        warnings.warn(
            "no ANTHROPIC_API_KEY and no 'claude' CLI on PATH — background Claude upgrade disabled",
            RuntimeWarning,
            stacklevel=2,
        )
        return None
    if name == "claude_api":
        from .claude_api import ClaudeApiProvider

        return ClaudeApiProvider(**kwargs)
    if name == "claude_cli":
        from .claude_cli import ClaudeCliProvider

        return ClaudeCliProvider(**kwargs)
    if name in ("googletranslatefree", "google"):
        from .googletranslatefree import GoogleTranslateFreeProvider

        return GoogleTranslateFreeProvider(**kwargs)
    raise ValueError(f"unknown translation provider: {name!r}")
