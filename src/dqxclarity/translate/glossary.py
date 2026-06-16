"""Proper-noun glossary: pin names/places/skills to their canonical English before MT.

DQX has thousands of proper nouns (people, places, monsters, skills) whose machine translation is
inconsistent and often wrong (a name reads three different ways across three lines). Upstream keeps
a curated ~40k-term ``glossary.csv`` (``ja,en``) and, *before* sending any string to the machine
translator, substitutes each known term with its canonical English — so the MT engine sees and
preserves the right name instead of mangling it. Community/cache hits are already correct and are
NEVER passed through here.

Ported from dqxclarity/app/common:
  * source/format — update.py:46-57 (glossary.csv lives under ``/csv/`` in the
    dqx-custom-translations repo zip) and update.py:211-217 (CSV rows are ``ja,en``, split on the
    FIRST comma so an English value may itself contain commas).
  * longest-first ordering — db_ops.py:95-100 (sort by JA-key byte length, longest first, so a
    longer name wins over a shorter one that is its prefix).
  * substitution — translate.py:41-52 (``__glossify``): replace ``ja`` with ``" en "`` (padded so
    two back-to-back hits don't fuse), collapse double spaces, strip leading space.

Performance: upstream does ~40k sequential ``str.replace`` calls *per translated phrase*, which is
far too slow for our live MT hot path. We instead compile ONE regex alternation of all keys (sorted
longest-first, each ``re.escape``-d) and substitute in a single pass; the compiled matcher is cached
on the :class:`Glossary` so the per-call cost is one regex scan, not 40k scans.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from collections.abc import Iterable, Mapping
from pathlib import Path

# Same archive upstream reads glossary.csv out of (dqx-custom-translations repo zip). update.py:46
# globs ``/csv/`` members; the glossary file is named ``glossary.csv`` and may also live under
# ``generate_glossary/`` — we accept either location.
CUSTOM_TRANSLATIONS_ZIP_URL = (
    "https://github.com/dqx-translation-project/dqx-custom-translations/archive/refs/heads/main.zip"
)
# Any ``.../csv/glossary.csv`` or ``.../generate_glossary/glossary.csv`` member in the repo archive.
_GLOSSARY_RE = re.compile(r"(?:/csv/|/generate_glossary/).*glossary\.csv$")


def _is_corrupt_row(ja: str, en: str) -> bool:
    """True for a glossary row whose English value is poisoned data we must not substitute.

    The upstream glossary has a few corrupt rows whose EN is nothing but quote characters — e.g.
    ``と頼まれた。`` ("...I was asked.") -> ``""""""``. Those JA keys are ubiquitous sentence
    endings, so glossifying them injects six literal quotes into the MT source and shreds the
    output (this was the dialogue-corruption bug). A real glossary value is a name/word and is
    never bare quotes, so dropping quote-only EN is surgical: it removes exactly the poison and
    leaves legitimate rows (including JA->JA speech normalizations like ``ワガハイ``->``わがはい``)
    untouched.
    """
    stripped = en.strip()
    return bool(stripped) and all(c in "\"'`" for c in stripped)


class Glossary:
    """An ordered (longest-JA-first) term map with a single cached compiled matcher.

    Empty/unavailable glossaries are a safe no-op: :meth:`glossify` returns its input unchanged, so
    a failed download (offline) never breaks translation.
    """

    def __init__(self, terms: Iterable[tuple[str, str]] = ()) -> None:
        # Preserve first occurrence, drop duplicate/empty JA keys, then sort longest-first by the
        # JA byte length (db_ops.py:95-100) so a longer name beats a shorter one it contains.
        seen: dict[str, str] = {}
        for ja, en in terms:
            if ja and en and ja not in seen:
                seen[ja] = en
        self._terms: list[tuple[str, str]] = sorted(
            seen.items(), key=lambda kv: len(kv[0].encode("utf-8")), reverse=True
        )
        self._repl: dict[str, str] = dict(self._terms)
        # Compile the matcher eagerly at construction. Building the ~40k-key alternation lazily on
        # the first glossify() added a ~315 ms stutter to the first live MT; doing it here moves that
        # one-time cost to load_glossary() (off the hot path). An empty glossary stays None (no-op).
        self._matcher: re.Pattern[str] | None = None
        if self._terms:
            # Keys are already longest-first; re.escape() neutralizes any regex-meta chars in a JA
            # key (rare, but a literal '(' / '*' / '?' must match literally, not as a metachar). The
            # regex engine tries alternatives left-to-right, so longest-first ordering makes a longer
            # term win over a shorter prefix in one pass.
            alternation = "|".join(re.escape(ja) for ja, _ in self._terms)
            self._matcher = re.compile(alternation)

    def __len__(self) -> int:
        return len(self._terms)

    def __bool__(self) -> bool:
        return bool(self._terms)

    def glossify(self, text: str) -> str:
        """Substitute every known JA term with ``" en "``; collapse doubled spaces; lstrip.

        Mirrors __glossify (translate.py:41-52). No-op (returns ``text`` unchanged) when the
        glossary is empty/unavailable, so it is always safe on the MT hot path. The matcher is
        compiled at construction (``__init__``), so this is one regex scan with no first-call stutter.
        """
        matcher = self._matcher
        if matcher is None or not text:
            return text
        # Pad each replacement with spaces so two back-to-back names don't fuse (translate.py:45-46).
        out = matcher.sub(lambda m: f" {self._repl[m.group(0)]} ", text)
        # Collapse the doubled spaces those pads introduce, then drop the leading space
        # (translate.py:48-50). Use a regex so 3+ runs (adjacent hits) also collapse to one.
        out = re.sub(r" {2,}", " ", out)
        return out.lstrip()

    def reference_hits(self, text: str) -> dict[str, str]:
        """Glossary terms PRESENT in text, as {ja: en}, WITHOUT substituting (for MT reference context)."""
        if self._matcher is None or not text:
            return {}
        return {m.group(0): self._repl[m.group(0)] for m in self._matcher.finditer(text)}


def glossify(text: str, glossary: Glossary | None) -> str:
    """Module-level convenience: glossify ``text`` with ``glossary`` (no-op if it's None/empty)."""
    if glossary is None:
        return text
    return glossary.glossify(text)


def parse_glossary_csv(data: bytes | str) -> list[tuple[str, str]]:
    """Parse glossary.csv bytes/str into ``(ja, en)`` rows.

    Each row is split on the FIRST comma (``str.split(",", 1)``) so an English value containing
    commas survives intact — everything after the first comma is the English (update.py:216-217).
    Blank lines and rows without a comma are skipped; rows whose JA or EN is empty after stripping
    are dropped.

    Quoted CSV fields are NOT supported: this is a plain first-comma split, not an RFC-4180 parse,
    so a JA key containing a comma is unsupported (its tail would be mis-read as part of the EN).
    The upstream glossary keys never contain commas, so this matches the real data.
    """
    text = data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else data
    rows: list[tuple[str, str]] = []
    for record in text.split("\n"):
        record = record.rstrip("\r")
        if not record or "," not in record:
            continue
        ja, en = record.split(",", 1)
        ja, en = ja.strip(), en.strip()
        if ja and en and not _is_corrupt_row(ja, en):
            rows.append((ja, en))
    return rows


def extract_glossary_from_zip(content: bytes) -> list[tuple[str, str]]:
    """Find and parse ``glossary.csv`` inside a dqx-custom-translations repo archive zip.

    Returns the parsed ``(ja, en)`` rows, or ``[]`` if the archive has no glossary member (so a
    missing file degrades to an empty, no-op glossary rather than raising). Mirrors update.py:46-57,
    which globs the ``/csv/`` members and reads the one named ``glossary.csv``.
    """
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        for name in zf.namelist():
            if _GLOSSARY_RE.search(name):
                return parse_glossary_csv(zf.read(name))
    return []


def _cache_path(cache_dir: Path) -> Path:
    return cache_dir / "glossary.csv"


def load_glossary(
    cache_dir: Path | None = None,
    *,
    url: str = CUSTOM_TRANSLATIONS_ZIP_URL,
    download: bool = True,
) -> Glossary:
    """Load the proper-noun glossary, preferring a local snapshot, refreshing from the repo zip.

    Resolution order (each step is best-effort so this never raises into the MT path):
      1. If ``cache_dir`` holds a saved ``glossary.csv``, load it — avoids a re-download every run
         (persistence requirement; the glossary is large and changes rarely).
      2. Otherwise, if ``download`` is set, fetch the dqx-custom-translations repo zip, extract
         ``glossary.csv``, and persist it next to the cache for next time.
      3. On any failure (offline, malformed archive) return an EMPTY glossary — glossify is then a
         safe no-op and translation proceeds untouched (the offline guard).
    """
    if cache_dir is not None:
        local = _cache_path(cache_dir)
        if local.exists():
            try:
                return Glossary(parse_glossary_csv(local.read_bytes()))
            except OSError:
                pass  # unreadable snapshot -> fall through to (re)download / empty

    if not download:
        return Glossary()

    try:
        import httpx

        resp = httpx.get(url, timeout=180.0, follow_redirects=True)
        resp.raise_for_status()
        rows = extract_glossary_from_zip(resp.content)
    except Exception:  # noqa: BLE001 - offline / network / archive errors must not break MT
        return Glossary()

    if cache_dir is not None and rows:
        try:
            save_glossary(rows, cache_dir)
        except OSError:
            pass  # persistence is best-effort; an in-memory glossary still works this run
    return Glossary(rows)


def save_glossary(rows: Iterable[tuple[str, str]] | Mapping[str, str], cache_dir: Path) -> Path:
    """Persist glossary rows as ``glossary.csv`` next to the cache so the next run skips the fetch.

    Written as ``ja,en`` (the same shape upstream's CSV uses) so :func:`parse_glossary_csv` can read
    it back. Returns the file path.
    """
    items: Iterable[tuple[str, str]] = (
        rows.items() if isinstance(rows, Mapping) else rows
    )
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _cache_path(cache_dir)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        for ja, en in items:
            writer.writerow([ja, en])
    return path


def sync_glossary(cache_dir: Path, *, url: str = CUSTOM_TRANSLATIONS_ZIP_URL) -> int:
    """Download glossary.csv from the custom-translations repo zip and persist it. Returns term count.

    The sync entry point used by the ``sync`` command so the glossary refreshes alongside the rest
    of the community data. Raises on download failure (the CLI catches and reports it), unlike
    :func:`load_glossary` which must stay a no-op on the hot path.
    """
    import httpx

    resp = httpx.get(url, timeout=180.0, follow_redirects=True)
    resp.raise_for_status()
    rows = extract_glossary_from_zip(resp.content)
    save_glossary(rows, cache_dir)
    return len(rows)
