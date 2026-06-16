"""Composite Claude provider: try the API, fall back to the CLI per item on any miss.

The single resolver in :mod:`.__init__` builds this when BOTH an ``ANTHROPIC_API_KEY`` and the
``claude`` CLI are available, so a runtime API hiccup (rate-limit, 5xx, network blip, an item the
model dropped) doesn't strand a line at google quality — the CLI retries exactly the items the API
missed. When only one transport is available the resolver returns that provider directly (no
wrapper), so this composite always has a real primary AND fallback.

Fallback is per ITEM, not per batch: ``translate``/``translate_rich`` return ``[str | None]``, and any
``None`` entry (whether the whole call errored — all None — or the model dropped a few) is re-sent to
the fallback and merged back in. An item both providers fail stays ``None`` (left for a later pass).

``name`` is ``"claude_api"`` deliberately: it must be a rank-2 source (db.rank_of) so a line the
worker successfully upgrades is NOT re-queued forever — a rank-1 label would satisfy
``rank_of(source) < rank_of(bg)`` on every pass. Whether the API or the CLI actually produced a given
line, both are rank 2, so the single label is correct for ranking (only cosmetically imprecise).
"""

from __future__ import annotations

from typing import Callable

from .base import Provider


class ClaudeFallbackProvider:
    name = "claude_api"  # rank-2 (see module docstring) — must not be a rank-1 label

    def __init__(self, primary: Provider, fallback: Provider) -> None:
        self.primary = primary
        self.fallback = fallback

    def available(self) -> bool:
        return self.primary.available() or self.fallback.available()

    def translate(self, texts: list[str]) -> list[str | None]:
        return self._merge(texts, self.primary.translate, self.fallback.translate)

    def translate_rich(self, items: list[dict]) -> list[str | None]:
        return self._merge(items, self.primary.translate_rich, self.fallback.translate_rich)

    @staticmethod
    def _merge(
        batch: list,
        primary_fn: Callable[[list], list[str | None]],
        fallback_fn: Callable[[list], list[str | None]],
    ) -> list[str | None]:
        if not batch:
            return []
        res = list(primary_fn(batch))
        missing = [i for i, r in enumerate(res) if r is None]
        if missing:
            fb = fallback_fn([batch[i] for i in missing])
            # zip (not index) so a fallback that returns a short/odd-length list can't IndexError —
            # any item the fallback omits simply stays None (left for a later pass).
            for i, r in zip(missing, fb):
                res[i] = r
        return res
