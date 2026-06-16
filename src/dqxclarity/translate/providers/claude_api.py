"""Translation provider that calls the Anthropic Messages HTTP API directly (via httpx).

The metered-API sibling of :mod:`.claude_cli`: same prompts, same parsers, same fail-safe contract —
only the transport differs. Used for the slow, background UPGRADE pass when an ``ANTHROPIC_API_KEY``
is present (the single resolver in :mod:`.__init__` prefers this over the CLI). We call the HTTP API
directly with httpx (already a dependency); the heavyweight ``anthropic`` SDK is intentionally NOT
used.

Failures (no key, non-2xx, network error, unparseable body) return None per item — the caller then
leaves the text untranslated rather than blocking gameplay. This provider NEVER raises.
"""

from __future__ import annotations

import json
import os

import httpx

from ._claude_common import (
    _SYSTEM,
    _SYSTEM_RICH,
    _parse,
    _parse_rich,
)

_API_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"
_DEFAULT_MODEL = "claude-sonnet-4-6"


class ClaudeApiProvider:
    name = "claude_api"

    def __init__(self, *, model: str = "", timeout: float = 120.0) -> None:
        self._key = os.environ.get("ANTHROPIC_API_KEY")
        self.model = model or _DEFAULT_MODEL
        self.timeout = timeout
        self._client = httpx.Client(timeout=timeout)

    def available(self) -> bool:
        return bool(self._key)

    def translate(self, texts: list[str]) -> list[str | None]:
        if not texts:
            return []
        if not self._key:
            return [None] * len(texts)
        text = self._call(_SYSTEM, texts, len(texts))
        if text is None:
            return [None] * len(texts)
        return _parse(text, len(texts))

    def translate_rich(self, items: list[dict]) -> list[str | None]:
        """Rich-context batch translate: each item carries ja + glossary/names/baseline/surface.

        Mirrors :meth:`translate`'s guards and HTTP handling, but uses the rich DQX system prompt
        (``_SYSTEM_RICH``) and the object-tolerant :func:`_parse_rich`. Detected by the worker via
        ``hasattr(bg, "translate_rich")``.
        """
        if not items:
            return []
        if not self._key:
            return [None] * len(items)
        text = self._call(_SYSTEM_RICH, items, len(items))
        if text is None:
            return [None] * len(items)
        return _parse_rich(text, len(items))

    def _call(self, system: str, payload: object, n: int) -> str | None:
        """POST to the Messages API; return the first text block's text, or None on any failure.

        NEVER raises: any httpx error, non-2xx status, malformed JSON, or missing/empty text block
        degrades to None so the caller falls back to ``[None] * n``.
        """
        headers = {
            "x-api-key": self._key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        body = {
            "model": self.model,
            "max_tokens": min(8192, 200 * n + 1024),
            "system": system,
            "messages": [
                {
                    "role": "user",
                    "content": f"Input:\n{json.dumps(payload, ensure_ascii=False)}",
                }
            ],
        }
        # NEVER raise: catch EVERYTHING and degrade to None. This contract is load-bearing for the
        # fallback composite — if this raised, ClaudeFallbackProvider.primary would raise and the CLI
        # fallback would never run (the worker's blanket except would kill the whole batch instead).
        try:
            resp = self._client.post(_API_URL, headers=headers, json=body)
            if resp.status_code // 100 != 2:
                return None
            data = resp.json()
            for block in data["content"]:
                if block.get("type") == "text":
                    text = block.get("text")
                    if text is not None:
                        return text
            return None
        except Exception:  # noqa: BLE001 - any transport/parse/shape failure -> None (see above)
            return None
