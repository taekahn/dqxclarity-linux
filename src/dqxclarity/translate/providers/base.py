"""Provider protocol."""

from __future__ import annotations

from typing import Protocol


class Provider(Protocol):
    name: str

    def translate(self, texts: list[str]) -> list[str | None]:
        """Translate a batch of JA strings to EN.

        Returns a list aligned with ``texts``; an entry is None if that string couldn't be
        translated (the caller leaves it as-is). Must never raise for a normal failure.
        """
        ...

    def available(self) -> bool:
        """Whether this provider can run right now (binary present, reachable, etc.)."""
        ...
