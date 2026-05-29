"""Translation orchestrator — two-tier: fast first-view + slow background quality upgrade.

Hot path (`lookup`) is in-memory only. `translate_now` uses a **fast** provider synchronously so
dialogue renders in English on first view, then enqueues the same text for a **slow,
higher-quality** provider that runs on a background thread and *upgrades* the cache entry (the
cache only ever moves up in quality: community > claude > google). So the next time a line
appears, it shows the better translation instantly.
"""

from __future__ import annotations

import queue
import re
import threading
import unicodedata

from .db import TranslationCache, rank_of
from .glossary import Glossary, glossify
from .providers.base import Provider
from .romanize import romanize
from .tags import protect_tags, restore_tags

# Honorifics the game's font/MT mangle when glued onto a name. Mirrors upstream's honorific list in
# translate.py:318 (__api_translate's name-tag honorific strip). Upstream strips these where they
# follow a name *tag* (<pc>/<cs_pchero>/<kyodai>); we have the literal name in the text instead, so
# we strip the honorific only where it immediately follows the KNOWN player/sibling name.
_NAME_HONORIFICS = ["さま", "君", "どの", "ちゃん", "くん", "様", "さーん", "殿", "さん"]

# Targeted glyph swaps for _normalize_mt_output, applied BEFORE the NFKD/ascii fold so dashes,
# quotes and the ellipsis survive as ASCII punctuation instead of being dropped (translate.py:54-60
# only does the final fold; we add this pass in front). Module-level so it isn't rebuilt per call.
_MT_OUTPUT_REPLACEMENTS = {
    "‘": "'",  # left single quote / curly apostrophe ‘
    "’": "'",  # right single quote / curly apostrophe ’
    "“": '"',  # left double quote “
    "”": '"',  # right double quote ”
    "—": "-",  # em-dash —
    "–": "-",  # en-dash –
    "…": "...",  # single-char ellipsis …
}


class Translator:
    def __init__(
        self,
        cache: TranslationCache,
        *,
        sync_provider: Provider | None = None,  # fast, synchronous (first-view)
        upgrade_provider: Provider | None = None,  # slow, background, higher quality
        romanize_names: bool = True,
        batch_size: int = 16,
        glossary: Glossary | None = None,
    ) -> None:
        self.cache = cache
        self.sync_provider = sync_provider
        self.upgrade_provider = upgrade_provider
        self.romanize_names = romanize_names
        self.batch_size = max(1, batch_size)
        # Proper-noun glossary applied to the JA input *before* MT only (never to community/cache
        # hits — those are already correct). Mirrors upstream, which glossifies each phrase right
        # before the machine translator in __api_translate (translate.py:220-221). None/empty makes
        # glossify a no-op, so an offline/missing glossary never breaks translation.
        self.glossary = glossary
        # LIVE player/sibling names for community-DB placeholder matching. The community lookup reads
        # these on EVERY call (not via a build-time closure over cfg) so the PLAYER hook can update
        # them at runtime — detection then applies WITHOUT a restart. Seeded from cfg by the CLI's
        # _build_translator; the player hook's apply_names mutates them in place.
        self.player_name_ja = ""
        self.player_name_en = ""
        self.sibling_name_ja = ""
        self.sibling_name_en = ""
        self.sibling_relationship = 0  # raw login byte (1-4); 0 = unknown
        # Compiled honorific-strip patterns, lazily (re)built when the (player, sibling) name pair
        # changes — the PLAYER hook can swap the names at runtime, so we key the cache on the names.
        self._honorific_key: tuple[str, str] | None = None
        self._honorific_patterns: list[re.Pattern[str]] = []
        self._q: queue.Queue[str] = queue.Queue()
        self._inflight: set[str] = set()
        self._inflight_lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()

    # ----- hot path ------------------------------------------------------- #
    def lookup(self, ja: str) -> str | None:
        """In-memory cache lookup only (safe on the game thread)."""
        return self.cache.lookup(ja)

    def translate_name(self, ja: str) -> str:
        """Resolve a player/NPC name: cache → local romaji. Always returns something."""
        hit = self.cache.lookup(ja)
        if hit is not None:
            return hit
        en = romanize(ja) if self.romanize_names else ja
        self.cache.store(ja, en, "romaji")
        return en

    # ----- MT-output / MT-input polish (machine-translation path ONLY) ---- #
    @staticmethod
    def _normalize_mt_output(text: str) -> str:
        """Fold provider output to characters the DQX font can actually render (GAP #22).

        Google/DeepL hand back glyphs the in-game font draws as blanks/boxes: curly apostrophes &
        quotes, em/en dashes, the single-char ellipsis, and accented latin. We do TARGETED swaps
        first (so an em-dash becomes ``-`` instead of being deleted), THEN NFKD-fold whatever
        accents remain to ASCII (so ``café`` -> ``cafe``). Mirrors upstream's __normalize_text
        (translate.py:54-60), which only does the final NFKD/ascii-ignore fold; we add the targeted
        pass in front so dashes/quotes survive as ASCII punctuation. Applied to PROVIDER OUTPUT
        ONLY — never to community/cache hits or to the JA input.
        """
        # Targeted replacements BEFORE the NFKD fold (the fold would otherwise drop these entirely).
        for src, dst in _MT_OUTPUT_REPLACEMENTS.items():
            text = text.replace(src, dst)
        # NFKD-fold any remaining non-ascii (accents etc.) to ASCII (translate.py:54-60).
        return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()

    def _honorific_patterns_for(
        self, key: tuple[str, str]
    ) -> list[re.Pattern[str]]:
        """Compiled ``{name}{honorific}`` patterns for the current (player, sibling) name pair.

        Rebuilt only when the name pair changes (the PLAYER hook can swap names at runtime). Each
        pattern requires the name to NOT be preceded by another Japanese word character (negative
        lookbehind over hiragana/katakana/CJK + the prolonged-sound/iteration marks), so it strips
        only a *standalone* name occurrence — a name at string start, after a space, or after
        non-Japanese punctuation — and never a name that is merely the TAIL of a longer Japanese
        word (e.g. ``カン`` inside ``タイカン``).
        """
        if key != self._honorific_key:
            honorific_alt = "|".join(re.escape(h) for h in _NAME_HONORIFICS)
            self._honorific_patterns = [
                re.compile(
                    r"(?<![ぁ-んァ-ヴ一-鿿ーヽヾ々])"
                    + r"("
                    + re.escape(name)
                    + r")"
                    + r"(?:"
                    + honorific_alt
                    + r")"
                )
                for name in key
                if name
            ]
            self._honorific_key = key
        return self._honorific_patterns

    def _strip_name_honorifics(self, text: str) -> str:
        """Drop an honorific glued onto the KNOWN player/sibling name before MT (GAP #24).

        Upstream strips honorifics that follow a name *tag* so MT doesn't render ``-sama``/``Mr.``
        onto the name (translate.py:316-321). The tag can't appear mid-word, so the strip was
        inherently boundary-safe. We don't have a tag in the raw segment — the literal name is in
        the text — but we know the names (player_name_ja / sibling_name_ja). So we strip
        ``{name}{honorific}`` -> ``{name}`` only where the honorific *immediately follows a
        standalone occurrence of the known name* (e.g. ``タイカンさま`` -> ``タイカン``). The regex's
        negative lookbehind keeps us boundary-safe like upstream: a name preceded by another
        Japanese character (i.e. mid-word, such as ``カン`` in ``タイカン``) is left untouched, as are
        unrelated words like ``おじいさん``. No-op when both names are empty.
        """
        if not self.player_name_ja and not self.sibling_name_ja:
            return text
        key = (self.player_name_ja, self.sibling_name_ja)
        for pat in self._honorific_patterns_for(key):
            # Group 1 is the name; the honorific (the rest of the match) is dropped.
            text = pat.sub(r"\1", text)
        return text

    def translate_now(self, ja: str) -> str | None:
        """Synchronous fast translate for first-view; enqueues a background quality upgrade.

        Blocks until the fast provider returns. Returns None on miss/failure. If the cache already
        has an entry, returns it (and queues an upgrade if a better provider could improve it).
        """
        hit = self.cache.lookup(ja)
        if hit is not None:
            self.request_upgrade(ja)
            return hit
        if self.sync_provider is None:
            return None
        # Strip an honorific glued onto the known player/sibling name BEFORE MT (GAP #24,
        # translate.py:316-321) so the provider doesn't translate it into "-sama"/"Mr.", then
        # glossify the JA *only* on the way INTO the machine translator (translate.py:220-221) so it
        # sees canonical proper nouns. The cache stays keyed on the original ``ja`` (the key the game
        # presents), so the next lookup/community hit still matches and is never re-glossified.
        # Protect 40+ variable/color tags (and resolve <kyodai_rel*> to the English sibling word)
        # by swapping them to MT-proof sentinels BEFORE glossify/MT, then restore after (#14,
        # translate.py:323-328). Done after honorific-strip / before glossify so the protected
        # text still gets canonical proper nouns, and the sentinels keep the tags out of MT's reach.
        src = glossify(
            protect_tags(self._strip_name_honorifics(ja), self.sibling_relationship),
            self.glossary,
        )
        try:
            res = self.sync_provider.translate([src])
        except Exception:  # noqa: BLE001 - never propagate into the game thread
            res = None
        en = res[0] if res else None
        if en:
            # Fold provider output to font-renderable ASCII (GAP #22). Provider output ONLY — a
            # community/cache hit returns above via ``lookup`` and is never normalized.
            en = self._normalize_mt_output(en)
            # Restore the protected tags (typo-tolerant) AFTER the ASCII fold (#14) so the fold
            # can't strip a sentinel character; the sentinels are already ASCII and survive it.
            en = restore_tags(en)
            self.cache.store_if_better(ja, en, self.sync_provider.name)
            self.request_upgrade(ja)
        return en

    # ----- background translate/upgrade worker ---------------------------- #
    @property
    def _background_provider(self) -> "Provider | None":
        """Provider the background worker uses for ASYNC (sync=False) surfaces.

        Prefer the slow high-quality upgrade provider, but fall back to the fast SYNC provider when
        it's off. Without this, an async surface (e.g. the quest menu, sync=False) whose text isn't
        in the community DB queues a request that is NEVER fulfilled when the upgrade provider is
        disabled — the text stays Japanese forever. Falling back to the sync provider means async
        text still gets translated (just in the background, no first-view block).
        """
        return self.upgrade_provider or self.sync_provider

    def _wants_upgrade(self, ja: str) -> bool:
        bg = self._background_provider
        if bg is None:
            return False
        source = self.cache.source_of(ja)
        if source is None:
            return True  # not translated AT ALL -> the background worker must translate it (an async
            # surface left this uncovered). rank_of(None) defaults to 1, so the rank check below would
            # wrongly skip it when the background provider is also rank 1 (google) — this is the case.
        return rank_of(source) < rank_of(bg.name)  # already translated -> only re-do for a real upgrade

    def request_upgrade(self, ja: str) -> None:
        """Queue a string for a slow, higher-quality re-translation (no-op if not worthwhile)."""
        if not self._wants_upgrade(ja):
            return
        with self._inflight_lock:
            if ja in self._inflight:
                return
            self._inflight.add(ja)
        self._q.put(ja)

    # Back-compat alias for the async-only path.
    request = request_upgrade

    def start(self) -> None:
        if self._background_provider is None or self._worker is not None:
            return
        self._worker = threading.Thread(target=self._run, name="background-worker", daemon=True)
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()
        if self._worker:
            self._worker.join(timeout=2.0)

    def _run(self) -> None:
        bg = self._background_provider
        assert bg is not None
        while not self._stop.is_set():
            batch = self._drain_batch()
            if not batch:
                continue
            # Strip name honorifics (GAP #24, translate.py:316-321) then glossify each JA before the
            # slow MT call (same point as the sync path / upstream __api_translate,
            # translate.py:220-221) while caching under the original keys in ``batch`` so
            # cache/community lookups still match.
            to_translate = [
                glossify(
                    protect_tags(self._strip_name_honorifics(ja), self.sibling_relationship),
                    self.glossary,
                )
                for ja in batch
            ]
            try:
                results = bg.translate(to_translate)
            except Exception:  # noqa: BLE001 - provider must not kill the worker
                results = [None] * len(batch)
            for ja, en in zip(batch, results):
                if en:
                    # Fold provider output to font-renderable ASCII (GAP #22); provider output only,
                    # then restore the protected tags (typo-tolerant) AFTER the fold (#14).
                    self.cache.store_if_better(
                        ja, restore_tags(self._normalize_mt_output(en)), bg.name
                    )
            with self._inflight_lock:
                self._inflight.difference_update(batch)

    def _drain_batch(self) -> list[str]:
        try:
            first = self._q.get(timeout=0.5)
        except queue.Empty:
            return []
        batch = [first]
        while len(batch) < self.batch_size:
            try:
                batch.append(self._q.get_nowait())
            except queue.Empty:
                break
        return batch
