"""Local Japanese → romaji conversion for player/NPC names (no external service).

Player-generated names can't live in any curated DB, so we transliterate them in-process with
pykakasi (kana + kanji → romaji, using its bundled dictionary). Fast, offline, free.
"""

from __future__ import annotations

import functools


@functools.lru_cache(maxsize=1)
def _kks():
    import pykakasi

    return pykakasi.kakasi()


def romanize(text: str) -> str:
    """Convert a Japanese string to title-cased romaji (e.g. 'たろう' -> 'Tarou').

    Returns the input unchanged if pykakasi is unavailable or conversion yields nothing.
    """
    try:
        result = _kks().convert(text)
    except Exception:  # noqa: BLE001 - never let romanization break the caller
        return text
    parts = [item.get("hepburn", "") for item in result]
    out = " ".join(p for p in parts if p).strip()
    if not out:
        return text
    # Names read best capitalized; keep it simple and word-cap.
    return " ".join(w[:1].upper() + w[1:] if w else w for w in out.split(" "))


def is_available() -> bool:
    try:
        _kks()
        return True
    except Exception:  # noqa: BLE001
        return False
