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
import time
import unicodedata

from .db import TranslationCache, rank_of
from .glossary import Glossary, glossify
from .providers.base import Provider
from .romanize import romanize
from .tags import protect_tags, restore_tags, shield_name

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
        self.profiler = None  # optional runtime.profile.Profiler set by `run --profile` (times _run)
        # Compiled honorific-strip patterns, lazily (re)built when the (player, sibling) name pair
        # changes — the PLAYER hook can swap the names at runtime, so we key the cache on the names.
        self._honorific_key: tuple[str, str] | None = None
        self._honorific_patterns: list[re.Pattern[str]] = []
        self._q: queue.Queue[tuple[str, str | None]] = queue.Queue()
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

    def _shield_names(self, text: str) -> str:
        """Replace each literal player/sibling JA name with its MT-proof EN-name sentinel (GAP #25).

        The player's/sibling's LITERAL Japanese name appears inline in ordinary game text (e.g.
        ``タイカンは3600ゴールドを手に入れた！`` = "<name> received 3600 Gold!"). _strip_name_honorifics
        removes a trailing honorific but leaves the bare NAME exposed, and that bare JA name is then
        corrupted two ways on the MT path:
          1. glossify does naive substring substitution — a glossary term ``イカ`` -> "Squid" matches
             *inside* the name タ-**イカ**-ン, so ``glossify('タイカン')`` becomes ``'タ Squid ン'`` and MT
             reads it as "Squid Tan".
          2. the machine translator mangles the bare name itself (``タイカン`` -> "Taycan"/"Tycoon").
        So we swap each literal JA-name occurrence for the correct EN name wrapped in the name-shield
        sentinel (:func:`shield_name`, the same SENTINEL+word+SENTINEL trick the sibling word uses).
        glossify and MT then see the opaque sentinel — never the JA name, so they can't substring-
        match inside it or re-translate it — and :func:`restore_tags` un-wraps it back to the plain
        EN name on the way out.

        ORDER: this runs AFTER _strip_name_honorifics (the honorific is already gone, so the bare
        name is what's exposed) and BEFORE protect_tags/glossify (so glossify never sees the JA name).

        Boundary-safety mirrors _strip_name_honorifics: a name preceded by another Japanese word
        character (i.e. mid-word) is left untouched via the same negative lookbehind, so we only
        shield a *standalone* name occurrence. We process the LONGER name first so that when one
        name is a substring of the other we don't partially replace the longer name. No-op when both
        JA names are empty (the common case — names unknown until the PLAYER hook fires).
        """
        if not self.player_name_ja and not self.sibling_name_ja:
            return text
        # (ja, en) pairs for whichever names are known, longest JA first so a name that contains the
        # other as a substring is shielded whole before the shorter one can partial-match inside it.
        pairs = [
            (self.player_name_ja, self.player_name_en),
            (self.sibling_name_ja, self.sibling_name_en),
        ]
        pairs = [(ja, en) for ja, en in pairs if ja]
        pairs.sort(key=lambda p: len(p[0]), reverse=True)
        for ja_name, en_name in pairs:
            # Fall back to the JA name if no EN is known yet (still better behind a sentinel: MT
            # can't mangle it and glossify can't substring-match inside it).
            replacement = shield_name(en_name or ja_name)
            # Same boundary-safe negative lookbehind as _honorific_patterns_for: don't fire when the
            # name is the TAIL of a longer Japanese word (preceded by hiragana/katakana/CJK or the
            # prolonged-sound/iteration marks). A standalone name — at string start, after a space,
            # or after non-Japanese punctuation/digits — is shielded.
            pattern = re.compile(
                r"(?<![ぁ-んァ-ヴ一-鿿ーヽヾ々])" + re.escape(ja_name)
            )
            text = pattern.sub(lambda _m, r=replacement: r, text)
        return text

    def _prepare_for_mt(self, ja: str) -> str:
        """Build the MT-input string shared by the sync and background paths (single source of truth).

        Pipeline order, each step justified inline at its definition:
          1. _strip_name_honorifics — drop a honorific glued onto the known name (GAP #24).
          2. _shield_names — swap the now-bare literal JA name for its MT-proof EN-name sentinel
             (GAP #25) so glossify/MT can't substring-match inside it or re-translate it.
          3. protect_tags — swap the 40+ variable/color/sibling tags to MT-proof sentinels (#14).
          4. glossify — pin proper nouns to canonical English (translate.py:220-221).
        Factored into one helper so the sync ``translate_now`` and the background ``_run`` worker
        apply IDENTICAL pre-MT processing (they previously inlined the same chain; the name shield
        must be on BOTH call sites). restore_tags on the MT output un-wraps every sentinel above.
        """
        return glossify(
            protect_tags(
                self._shield_names(self._strip_name_honorifics(ja)),
                self.sibling_relationship,
            ),
            self.glossary,
        )

    def _prepare_for_claude(self, ja: str) -> tuple[str, dict[str, str]]:
        """Claude MT input: same as _prepare_for_mt but glossary becomes a REFERENCE, not a substitution."""
        protected = protect_tags(
            self._shield_names(self._strip_name_honorifics(ja)),
            self.sibling_relationship,
        )
        hits = self.glossary.reference_hits(protected) if self.glossary else {}
        return protected, hits

    def _build_claude_items(self, batch: list[tuple[str, str | None]]) -> list[dict]:
        """Build the rich-context items for the Claude provider — derivable purely from ``ja`` + state.

        Each item carries the protected JA (glossary as a REFERENCE, not substituted), the known
        player/sibling name pairs, and the rough google baseline we're upgrading (only when the
        current cache source is a rank-1 MT). ``surface`` is the per-line register hint threaded in
        from the dispatch closure (Increment 2); None when the caller didn't supply one.
        """
        names: dict[str, str] = {}
        if self.player_name_ja and self.player_name_en:
            names[self.player_name_ja] = self.player_name_en
        if self.sibling_name_ja and self.sibling_name_en:
            names[self.sibling_name_ja] = self.sibling_name_en
        items: list[dict] = []
        for ja, surface in batch:
            protected, hits = self._prepare_for_claude(ja)
            src = self.cache.source_of(ja)
            baseline = self.cache.lookup(ja) if src in ("googletranslatefree", "google") else None
            items.append(
                {"ja": protected, "glossary": hits, "names": names, "baseline": baseline, "surface": surface}
            )
        return items

    def translate_now(self, ja: str, *, surface: str | None = None) -> str | None:
        """Synchronous fast translate for first-view; enqueues a background quality upgrade.

        Blocks until the fast provider returns. Returns None on miss/failure. If the cache already
        has an entry, returns it (and queues an upgrade if a better provider could improve it).
        ``surface`` is the optional register hint threaded down to the background enqueue so the
        first-view upgrade carries the same surface label as a re-view would (Increment 2).
        """
        hit = self.cache.lookup(ja)
        if hit is not None:
            self.request_upgrade(ja, surface=surface)
            return hit
        if self.sync_provider is None:
            return None
        # Build the MT-input string: strip the name honorific (GAP #24), shield the literal player/
        # sibling name behind an MT-proof EN-name sentinel (GAP #25), protect the 40+ variable/color
        # tags (#14), then glossify proper nouns (translate.py:220-221) — see _prepare_for_mt for the
        # full ordering rationale. The cache stays keyed on the original ``ja`` (the key the game
        # presents), so the next lookup/community hit still matches and is never re-processed.
        src = self._prepare_for_mt(ja)
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
            self.request_upgrade(ja, surface=surface)
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

    def request_upgrade(self, ja: str, *, surface: str | None = None) -> None:
        """Queue a string for a slow, higher-quality re-translation (no-op if not worthwhile).

        ``surface`` is an optional register hint (e.g. "dialogue", "quest", "network_text (...)")
        threaded in from the dispatch closure; it rides the queue alongside ``ja`` and is surfaced to
        the rich Claude provider as ``item["surface"]``. The ``_inflight`` dedupe is keyed on ``ja``
        ONLY (never the tuple), so the same line is never queued twice regardless of its surface.
        """
        if not self._wants_upgrade(ja):
            return
        with self._inflight_lock:
            if ja in self._inflight:
                return
            self._inflight.add(ja)
        self._q.put((ja, surface))

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
            # Same pre-MT processing as the sync path (_prepare_for_mt: honorific strip GAP #24,
            # name shield GAP #25, tag protect #14, glossify translate.py:220-221) so the slow
            # provider gets identical input — the name shield MUST be applied here too, not just on
            # the sync path. Cache under the original keys in ``batch`` so cache/community lookups
            # still match.
            # A provider exposing translate_rich (claude_cli) gets the full rich-context channel:
            # clean(er) JA, glossary as REFERENCE (not substituted), player/sibling names, and the
            # rough google baseline — all derived here from ``ja`` + translator state (no queue
            # changes). A plain provider (Google) keeps the substituting _prepare_for_mt path.
            _t_mt = time.monotonic() if self.profiler is not None else 0.0
            try:
                if hasattr(bg, "translate_rich"):
                    results = bg.translate_rich(self._build_claude_items(batch))
                else:
                    results = bg.translate([self._prepare_for_mt(ja) for ja, _ in batch])
            except Exception:  # noqa: BLE001 - provider must not kill the worker
                results = [None] * len(batch)
            if self.profiler is not None:
                # Time the whole provider call. Mostly network/subprocess wait (GIL released), so a
                # big number here is NOT itself a game stall — but it tells us how busy/slow the
                # background upgrade lane is, and whether MT activity coincides with serve-idle gaps.
                self.profiler.record("mt", bg.name, time.monotonic() - _t_mt, f"batch={len(batch)}")
            for (ja, _surface), en in zip(batch, results):
                if en:
                    # Fold provider output to font-renderable ASCII (GAP #22); provider output only,
                    # then restore the protected tags (typo-tolerant) AFTER the fold (#14).
                    self.cache.store_if_better(
                        ja, restore_tags(self._normalize_mt_output(en)), bg.name
                    )
            with self._inflight_lock:
                self._inflight.difference_update(ja for ja, _ in batch)

    def _drain_batch(self) -> list[tuple[str, str | None]]:
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
