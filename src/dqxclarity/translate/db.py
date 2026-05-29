"""Translation cache: in-memory hot dict in front of SQLite (WAL) persistence.

Per PLAN §9, the game-thread hot path touches only the in-memory dict (~ns lookups, no I/O,
no lock); SQLite is the durable store, loaded into the dict at startup and written behind it.
Each unique JA string is translated once, ever, then served from cache forever.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS translations (
    ja      TEXT PRIMARY KEY,
    en      TEXT NOT NULL,
    source  TEXT NOT NULL,   -- 'community' | 'claude_cli' | 'googletranslatefree' | 'romaji'
    updated REAL NOT NULL
);
"""

# Quality ranking — a higher-rank source may overwrite a lower-rank one, never the reverse.
# Lets a fast first-view translation be upgraded later by a slower, higher-quality one.
# INVARIANT: every provider's `name` MUST appear here, or it silently defaults to rank 1 (which
# could let a weak provider "upgrade" nothing, or block a real upgrade). Keep in sync with
# translate/providers/. Tier 3 = curated/human, 2 = LLM, 1 = fast MT / romaji.
_RANK = {
    "community": 3, "curated": 3,
    "claude_api": 2, "claude_cli": 2, "openai": 2, "ollama": 2,
    "googletranslatefree": 1, "google": 1, "deepl": 1, "libretranslate": 1, "yandex": 1,
    "romaji": 1,
}


def rank_of(source: str | None) -> int:
    return _RANK.get(source or "", 1)


class TranslationCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._lock = threading.Lock()  # serialize writes (one writer)
        # Warm the in-memory hot cache from disk (value + source for quality decisions).
        self._hot: dict[str, str] = {}
        self._src: dict[str, str] = {}
        for ja, en, src in self._conn.execute("SELECT ja, en, source FROM translations"):
            self._hot[ja] = en
            self._src[ja] = src

    def __len__(self) -> int:
        return len(self._hot)

    def lookup(self, ja: str) -> str | None:
        """Hot-path lookup — in-memory only, no I/O."""
        return self._hot.get(ja)

    def source_of(self, ja: str) -> str | None:
        return self._src.get(ja)

    # All mutations hold ``_lock`` so the in-memory dicts and SQLite stay consistent and the
    # check-then-act in store_if_better is atomic. ``lookup``/``source_of`` read without the lock
    # (GIL makes a single dict read safe) so the game thread's hot path never blocks on a writer.
    def store_if_better(self, ja: str, en: str, source: str) -> bool:
        """Store only if ``source`` is at least as high-quality as the existing entry."""
        with self._lock:
            if ja in self._src and rank_of(source) < rank_of(self._src[ja]):
                return False
            self._store_locked(ja, en, source)
            return True

    def store(self, ja: str, en: str, source: str) -> None:
        with self._lock:
            self._store_locked(ja, en, source)

    def _store_locked(self, ja: str, en: str, source: str) -> None:
        """Insert/update one row. Caller must hold ``self._lock``."""
        self._conn.execute(
            "INSERT INTO translations(ja, en, source, updated) VALUES(?,?,?,?) "
            "ON CONFLICT(ja) DO UPDATE SET en=excluded.en, source=excluded.source, "
            "updated=excluded.updated",
            (ja, en, source, time.time()),
        )
        self._conn.commit()
        self._hot[ja] = en  # update memory last so a reader never sees newer mem than disk
        self._src[ja] = source

    def store_many(self, rows: list[tuple[str, str, str]]) -> None:
        """Bulk import (e.g. syncing the curated community DB). rows = (ja, en, source)."""
        now = time.time()
        with self._lock:
            self._conn.executemany(
                "INSERT INTO translations(ja, en, source, updated) VALUES(?,?,?,?) "
                "ON CONFLICT(ja) DO UPDATE SET en=excluded.en, source=excluded.source, "
                "updated=excluded.updated",
                [(ja, en, src, now) for ja, en, src in rows],
            )
            self._conn.commit()
            for ja, en, src in rows:
                self._hot[ja] = en
                self._src[ja] = src

    def close(self) -> None:
        with self._lock:
            self._conn.close()
