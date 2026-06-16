"""End-to-end behavioural tests for the translation pipeline (the highest-value area).

These tests exist because a real async bug slipped through a file that was 95% line-covered: a test
only checked that ``request()`` "doesn't raise" instead of asserting it actually *translates*. So
these tests deliberately exercise REAL config combinations end-to-end and assert the OBSERVABLE
result (a string is translated and CACHED, with the right source/rank), not just that nothing blew
up.

Three layers are covered:
  * the Translator directly (the 2x2 matrix of {upgrade ON/OFF} x {sync surface / async surface});
  * the MT-output polish (glossify + honorific-strip + char-normalize) actually landing in the cache;
  * the dispatch layer (build_translate_fn / build_name_translate_fn / build_network_translate_fn)
    with a real Translator + real TranslationCache + a minimal cfg stub.

NOTE ON STUB OUTPUT: a real MT provider returns *English*, and the pipeline folds provider output to
font-renderable ASCII (``_normalize_mt_output``) — which deliberately drops any leftover non-ASCII
(including Japanese). So the stub providers here return ASCII-only English (a marker plus a rank tag)
rather than echoing the JA input; echoing JA would be unrealistic and the normalize pass would strip
it. Tests that care about the *input* the provider saw inspect a recorded ``seen`` list instead.

No network, no game process: stub providers + tmp_path only (HARD RULE c).
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from dqxclarity.runtime.dispatch import (
    build_name_translate_fn,
    build_network_translate_fn,
    build_translate_fn,
)
from dqxclarity.translate.db import TranslationCache, rank_of
from dqxclarity.translate.glossary import Glossary
from dqxclarity.translate.pipeline import Translator
from dqxclarity.translate.wrap import normalize_source

# --------------------------------------------------------------------------- shared test helpers


class StubProvider:
    """A rank-tagged provider whose translate() is a pure function of the input.

    ``name`` decides the cache rank (db.rank_of): 'googletranslatefree' -> 1, 'claude_cli' -> 2.
    Every translate() call is recorded in ``.calls`` so a test can assert the provider was (or, for
    a cache/community hit, was NOT) invoked.
    """

    def __init__(self, name: str, transform):
        self.name = name
        self._transform = transform
        self.calls: list[list[str]] = []

    def available(self) -> bool:
        return True

    def translate(self, texts):
        self.calls.append(list(texts))
        return [self._transform(t) for t in texts]


def _suffixer(name: str, suffix: str) -> StubProvider:
    """A provider returning ASCII-only English ``"EN" + suffix`` so a test can tell which tier ran.

    Output is intentionally ASCII (no JA echo): the pipeline's _normalize_mt_output folds provider
    output to ASCII and would strip any Japanese, so an English-only stub is the realistic shape.
    The rank-identifying ``suffix`` survives the fold and lets the test prove which provider ran.
    """
    return StubProvider(name, lambda t: "EN" + suffix)


def _cfg(**over):
    """Minimal config stub: the dispatch builders read only cfg.translate.* attributes.

    Pins network_translate_all=False so the network_text routing tests here exercise the LEGACY
    whitelist path (sync-provider inline MT, non-whitelisted -> pass through). The new "translate the
    rest" model (default True) is covered in tests/test_network_text.py.
    """
    tr = SimpleNamespace(
        player_name_ja="", player_name_en="", sibling_name_ja="", sibling_name_en="",
        wrap_width=46, lines_per_page=3, battle_names=True, network_translate_all=False,
    )
    for k, v in over.items():
        setattr(tr, k, v)
    return SimpleNamespace(translate=tr)


def _wait_until(predicate, timeout: float = 3.0) -> bool:
    """Poll ``predicate`` until true or ``timeout`` (the background worker runs on its own thread)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# ===========================================================================================
# The 2x2 matrix: {upgrade provider ON / OFF} x {sync surface / async surface}.
#
# In EVERY cell an UNCOVERED string must end up TRANSLATED and CACHED. The cell the async bug hid
# in is {upgrade OFF, async surface}: it must still translate via the sync provider, not stay JA.
# ===========================================================================================


def test_matrix_sync_surface_upgrade_off_translates_rank1(tmp_path):
    """sync surface + upgrade OFF: first-view translates rank-1 and does NOT enqueue an upgrade."""
    c = TranslationCache(tmp_path / "c.db")
    sync = _suffixer("googletranslatefree", "-s1")  # rank 1
    t = Translator(c, sync_provider=sync, upgrade_provider=None)
    t.start()
    out = t.translate_now("ねこ")
    assert out == "EN-s1"                                # translated synchronously on first view
    assert c.lookup("ねこ") == "EN-s1"                   # and cached
    assert c.source_of("ねこ") == "googletranslatefree"  # at rank 1
    # No upgrade provider -> nothing should ever overwrite it; give the (idle) worker a beat.
    time.sleep(0.1)
    t.stop()
    assert c.source_of("ねこ") == "googletranslatefree"  # still rank 1, never re-queued/upgraded
    assert len(sync.calls) == 1                          # exactly one provider call (no re-translate)
    c.close()


def test_matrix_sync_surface_upgrade_on_upgrades_rank(tmp_path):
    """sync surface + upgrade ON: first-view is rank-1, the worker upgrades it to rank-2."""
    c = TranslationCache(tmp_path / "c.db")
    sync = _suffixer("googletranslatefree", "-s1")     # rank 1
    upgrade = _suffixer("claude_cli", "-u2")           # rank 2
    t = Translator(c, sync_provider=sync, upgrade_provider=upgrade)
    t.start()
    assert t.translate_now("いぬ") == "EN-s1"            # fast first-view (rank 1)
    assert _wait_until(lambda: c.source_of("いぬ") == "claude_cli")  # upgraded in the background
    t.stop()
    assert c.lookup("いぬ") == "EN-u2"                    # the higher-quality text replaced rank 1
    assert c.source_of("いぬ") == "claude_cli"
    assert rank_of(c.source_of("いぬ")) == 2
    c.close()


def test_matrix_async_surface_upgrade_off_translates_via_sync_provider(tmp_path):
    """async surface + upgrade OFF: THE BUG CELL.

    An async surface (sync=False, e.g. the quest menu) queues request() for the background worker.
    With the upgrade provider OFF, the worker must FALL BACK to the sync provider so the uncovered
    string still gets translated — otherwise it stays Japanese forever (this is exactly the class of
    bug that slipped through: ``request()`` not raising told us nothing about whether it translated).
    """
    c = TranslationCache(tmp_path / "c.db")
    sync = _suffixer("googletranslatefree", "-bg")     # rank 1; the ONLY provider
    t = Translator(c, sync_provider=sync, upgrade_provider=None)
    t.start()
    assert c.source_of("みず") is None                   # uncovered to start
    t.request("みず")                                    # what dialogue's async path does on a miss
    assert _wait_until(lambda: c.lookup("みず") is not None)
    t.stop()
    assert c.lookup("みず") == "EN-bg"                    # translated in the background (not stuck JA)
    assert c.source_of("みず") == "googletranslatefree"  # via the sync provider, at rank 1
    assert sync.calls == [["みず"]]                       # the worker really invoked the sync provider
    c.close()


def test_matrix_async_surface_upgrade_on_translates_rank2(tmp_path):
    """async surface + upgrade ON: the worker translates the uncovered string at rank-2 directly."""
    c = TranslationCache(tmp_path / "c.db")
    sync = _suffixer("googletranslatefree", "-s1")     # rank 1
    upgrade = _suffixer("claude_cli", "-u2")           # rank 2 — preferred by the worker
    t = Translator(c, sync_provider=sync, upgrade_provider=upgrade)
    t.start()
    t.request("そら")
    assert _wait_until(lambda: c.lookup("そら") is not None)
    t.stop()
    assert c.lookup("そら") == "EN-u2"                    # the upgrade provider did the work
    assert c.source_of("そら") == "claude_cli"
    assert sync.calls == []                              # sync provider never touched on the async path
    assert upgrade.calls == [["そら"]]
    c.close()


def test_async_worker_without_any_upgrade_does_not_re_queue_forever(tmp_path):
    """Regression: once an async-translated string is cached at rank 1, it is NOT re-queued.

    With no upgrade provider, _wants_upgrade must return False for an already-cached rank-1 entry
    (rank_of(rank1) < rank_of(rank1) is False), so the worker translates it exactly once.
    """
    c = TranslationCache(tmp_path / "c.db")
    sync = _suffixer("googletranslatefree", "-once")
    t = Translator(c, sync_provider=sync, upgrade_provider=None)
    t.start()
    t.request("つき")
    assert _wait_until(lambda: c.lookup("つき") == "EN-once")
    # Ask again now that it's cached at rank 1: no better provider exists, so no second translate.
    t.request("つき")
    time.sleep(0.2)
    t.stop()
    assert sync.calls == [["つき"]]                       # translated exactly once, never re-queued
    c.close()


# ===========================================================================================
# A community / cache HIT is served WITHOUT calling any provider — both sync and async surfaces.
# ===========================================================================================


def test_cache_hit_sync_surface_skips_provider(tmp_path):
    """translate_now on a cache hit returns the cached value and never calls the sync provider."""
    c = TranslationCache(tmp_path / "c.db")
    c.store("こんにちは", "Hello", "community")            # rank 3, already perfect
    sync = StubProvider("googletranslatefree", lambda t: "SHOULD-NOT-BE-CALLED")
    t = Translator(c, sync_provider=sync, upgrade_provider=None)
    assert t.translate_now("こんにちは") == "Hello"        # served from cache
    assert sync.calls == []                               # provider untouched on a hit
    c.close()


def test_cache_hit_async_surface_skips_provider(tmp_path):
    """request() on a community-DB hit must not enqueue MT (rank-3 can't be upgraded)."""
    c = TranslationCache(tmp_path / "c.db")
    c.store("さようなら", "Goodbye", "community")           # rank 3
    sync = StubProvider("googletranslatefree", lambda t: "SHOULD-NOT-BE-CALLED")
    upgrade = StubProvider("claude_cli", lambda t: "SHOULD-NOT-BE-CALLED")
    t = Translator(c, sync_provider=sync, upgrade_provider=upgrade)
    t.start()
    t.request("さようなら")                                # async surface on a community hit
    time.sleep(0.2)                                       # give the worker a chance to (wrongly) run
    t.stop()
    assert sync.calls == [] and upgrade.calls == []       # no provider invoked — community wins
    assert c.lookup("さようなら") == "Goodbye"             # unchanged, still the curated EN
    assert c.source_of("さようなら") == "community"
    c.close()


def test_dialogue_cache_hit_queues_quality_upgrade(tmp_path):
    """A dialogue fragment cached at rank-1 (google) must be UPGRADED to claude on re-view.

    Regression for the dead upgrade-on-re-view path: the dialogue surface reaches the cache via
    _xlate -> translator.lookup() directly (not translate_now), so a cache HIT used to return the
    rank-1 text and NEVER enqueue an upgrade. Result: every already-cached dialogue line stayed at
    first-view (google) quality forever even with claude enabled. The fix requests an upgrade on the
    hit path; here the background worker must then flip the entry to claude_cli.
    """
    from dqxclarity.translate.dialogue import translate_conversation

    ja = "「ここはジュレットの町。"
    key = normalize_source(ja)
    c = TranslationCache(tmp_path / "c.db")
    c.store(key, "This is the town of Julet.", "googletranslatefree")  # rank 1, from a prior session
    sync = _suffixer("googletranslatefree", "-s1")
    upgrade = _suffixer("claude_cli", "-c2")  # rank 2 -> strictly better
    t = Translator(c, sync_provider=sync, upgrade_provider=upgrade)
    t.start()

    # Re-view the line: served from cache (the existing google EN), sync provider NOT re-invoked.
    out = translate_conversation(t, ja, sync=True)
    assert out is not None and "Julet" in out          # rendered from the cache hit
    assert sync.calls == []                             # a hit must not re-call the sync provider

    # ...but the hit MUST have queued a background upgrade, which flips the entry to claude_cli.
    assert _wait_until(lambda: c.source_of(key) == "claude_cli"), c.source_of(key)
    assert c.lookup(key) == "EN-c2"
    t.stop()
    c.close()


# ===========================================================================================
# MT-output polish lands in the CACHE: glossify (input) + honorific-strip (input) + normalize (out).
# ===========================================================================================


def test_translate_now_caches_normalized_mt_output(tmp_path):
    """A provider that returns curly quotes / em-dash / ellipsis / accents -> cached as ASCII.

    The MT path folds provider output to font-renderable ASCII (GAP #22). This must be visible in
    the CACHE, not just the return value, because the cache is what the next view serves.
    """
    c = TranslationCache(tmp_path / "c.db")
    # provider hands back glyphs the in-game font can't draw.
    sync = StubProvider("googletranslatefree", lambda t: "He said “don’t go” — café…")
    t = Translator(c, sync_provider=sync, upgrade_provider=None)
    out = t.translate_now("かれは　いった。")
    expected = 'He said "don\'t go" - cafe...'
    assert out == expected                                # returned value normalized
    assert c.lookup("かれは　いった。") == expected         # AND the cached value is normalized
    assert all(ord(ch) < 128 for ch in c.lookup("かれは　いった。"))  # pure ASCII in the cache
    c.close()


def test_translate_now_glossifies_input_before_mt(tmp_path):
    """The JA is glossified (canonical proper nouns) on the way INTO the provider, keyed on raw JA."""
    c = TranslationCache(tmp_path / "c.db")
    seen: list[str] = []

    def transform(t):
        seen.append(t)              # record the EXACT string the provider was handed
        return "DONE"               # ASCII-only output (a real provider returns English)

    sync = StubProvider("googletranslatefree", transform)
    t = Translator(c, sync_provider=sync, glossary=Glossary([("スライム", "Slime")]))
    t.translate_now("スライムだ")
    assert seen == ["Slime だ"]                            # provider saw the GLOSSIFIED JA (スライム->Slime)
    # The cache key stays the ORIGINAL JA (the key the game presents), never the glossified form.
    assert c.lookup("スライムだ") == "DONE"
    assert c.lookup("Slime だ") is None                    # never keyed on the glossified input
    c.close()


def test_translate_now_strips_name_honorific_before_mt(tmp_path):
    """An honorific glued onto the KNOWN player name is stripped before MT (GAP #24)."""
    c = TranslationCache(tmp_path / "c.db")
    seen: list[str] = []

    def transform(t):
        seen.append(t)
        return "EN:" + t

    sync = StubProvider("googletranslatefree", transform)
    t = Translator(c, sync_provider=sync)
    t.player_name_ja = "タイカン"                          # the live player name
    t.player_name_en = "Taikan"                           # EN name used by the name shield (GAP #25)
    t.translate_now("タイカンさま、こんにちは")
    # honorific さま dropped before MT (GAP #24); the now-bare name is then shielded behind the
    # MT-proof EN-name sentinel (GAP #25) so glossify/MT can't touch it. <&7_ab> is shield_name's
    # sentinel; the surrounding text is otherwise unchanged.
    from dqxclarity.translate.tags import shield_name
    assert seen == [f"{shield_name('Taikan')}、こんにちは"]
    c.close()


def test_background_worker_normalizes_and_glossifies(tmp_path):
    """The async worker applies the same input glossify + output normalize as the sync path."""
    c = TranslationCache(tmp_path / "c.db")
    seen: list[str] = []

    def transform(t):
        seen.append(t)                          # record the glossified input
        return "A slime appears — café…"        # English w/ em-dash + accent + ellipsis (font-unsafe)

    upgrade = StubProvider("claude_cli", transform)
    t = Translator(c, sync_provider=None, upgrade_provider=upgrade,
                   glossary=Glossary([("スライム", "Slime")]))
    t.start()
    t.request("スライムあらわる")
    assert _wait_until(lambda: c.lookup("スライムあらわる") is not None)
    t.stop()
    assert seen == ["Slime あらわる"]                      # glossified input (スライム -> Slime)
    cached = c.lookup("スライムあらわる")
    assert cached == "A slime appears - cafe..."          # output normalized to ASCII punctuation
    assert all(ord(ch) < 128 for ch in cached)
    c.close()


# ===========================================================================================
# End-to-end through dispatch: build_translate_fn / build_name_translate_fn / build_network_*.
# ===========================================================================================


def test_dispatch_text_community_hit_swaps_names_no_provider(tmp_path):
    """build_translate_fn: a community hit returns curated EN with the EN player name swapped in.

    The stored line uses the <pnplacehold> placeholder; the lookup swaps the JA name in to match,
    and the EN name back into the result — all WITHOUT calling MT.
    """
    c = TranslationCache(tmp_path / "c.db")
    c.store("<pnplacehold>たちは　成功した！", "<pnplacehold> and co. succeeded!", "community")
    sync = StubProvider("googletranslatefree", lambda t: "SHOULD-NOT-BE-CALLED")
    t = Translator(c, sync_provider=sync)
    cfg = _cfg(player_name_ja="タイカン", player_name_en="Taikan")
    fn, _ = build_translate_fn(cfg, t, sync=True)
    assert fn("タイカンたちは　成功した！") == "Taikan and co. succeeded!"
    assert sync.calls == []                               # community hit never touches the provider
    c.close()


def test_dispatch_text_miss_sync_returns_mt_and_caches(tmp_path):
    """build_translate_fn (sync): a miss machine-translates inline and caches the normalized result.

    The community lookup misses (raw JA not curated), so the dialogue MT path runs synchronously,
    normalizes the provider's curly-quote output, and stores it under the NORMALIZED source key.
    """
    c = TranslationCache(tmp_path / "c.db")
    sync = StubProvider("googletranslatefree", lambda t: "It’s done — really")  # curly quote + em-dash
    t = Translator(c, sync_provider=sync)
    fn, _ = build_translate_fn(_cfg(), t, sync=True)
    ja = "あたらしい　ものがたり。"
    out = fn(ja)
    assert out is not None and "It's done - really" in out  # normalized (curly quote/em-dash -> ASCII)
    assert all(ord(ch) < 128 for ch in out)
    # The dialogue path keys the cache on the normalized source, not the raw JA.
    norm = normalize_source(ja)
    assert c.lookup(norm) is not None
    assert c.source_of(norm) == "googletranslatefree"
    assert sync.calls                                     # the provider really ran
    c.close()


def test_dispatch_text_miss_async_returns_none_then_caches(tmp_path):
    """build_translate_fn (sync=False): a miss returns None first, then the worker caches it.

    This is the async surface (quest menu). With the upgrade provider OFF it must STILL translate
    (via the sync provider) — the exact dimension the async bug hid in, now asserted end-to-end.
    """
    c = TranslationCache(tmp_path / "c.db")
    sync = _suffixer("googletranslatefree", "-bg")
    t = Translator(c, sync_provider=sync, upgrade_provider=None)
    t.start()
    fn, _ = build_translate_fn(_cfg(), t, sync=False)
    ja = "クエストの　せつめい。"
    norm = normalize_source(ja)
    assert fn(ja) is None                                 # async miss -> None (queued), not partial JA
    assert _wait_until(lambda: c.lookup(norm) is not None)
    t.stop()
    assert c.source_of(norm) == "googletranslatefree"     # background-translated via the sync provider
    # The very next call now resolves to the cached EN (no longer None).
    fn2, _ = build_translate_fn(_cfg(), t, sync=False)
    assert fn2(ja) is not None and "-bg" in fn2(ja)
    c.close()


def test_dispatch_name_community_hit_then_romaji_fallback(tmp_path):
    """build_name_translate_fn: community hit wins; else romaji; non-JA passes through as None.

    The NAMEPLATES surface passes prefix='\\x04'; it is prepended only to a real replacement, never
    to a pass-through None. Names never go through MT.
    """
    c = TranslationCache(tmp_path / "c.db")
    c.store("スライム", "Slime", "community")              # a curated monster name (rank 3)
    t = Translator(c, sync_provider=StubProvider("googletranslatefree", lambda t: "MT!"))
    name_fn = build_name_translate_fn(_cfg(), t, prefix="\x04")
    assert name_fn("スライム") == "\x04Slime"              # curated hit, prefixed
    assert name_fn("たろう") == "\x04Tarou"                # no hit -> offline romaji, prefixed
    assert name_fn("Hello") is None                        # non-JA -> pass through (no prefix alone)
    # Names never hit the machine translator.
    assert t.sync_provider.calls == []
    c.close()


def test_dispatch_network_routing_name_vs_text_vs_passthrough(tmp_path):
    """build_network_translate_fn: category-aware routing (name path, text path, pass-through).

    Built with the REAL network_text surface profile (lines_per_page=0, sync surface) — the same
    args cli.py passes from the network_text HookSpec — so the routing is exercised as it ships.
    """
    c = TranslationCache(tmp_path / "c.db")
    c.store("スライム", "Slime", "community")              # name-category community hit
    sync = _suffixer("googletranslatefree", "-EN")
    t = Translator(c, sync_provider=sync)
    fn = build_network_translate_fn(_cfg(), t, lines_per_page=0, sync=True)

    # 1. NAME category -> name path (community hit, no MT).
    assert fn("スライム", "<%sM_pc>") == "Slime"
    # 2. A NET_IGNORE (battle/UI noise) category -> pass through unchanged.
    assert fn("こうげき", "<%sB_ACTION>") is None
    # 3. An unknown / non-whitelisted category -> pass through (this is what stops battle text MT).
    assert fn("なにか", "<%sTotallyUnknown>") is None
    # 4. non-Japanese -> None regardless of category.
    assert fn("Hello", "<%sM_pc>") is None
    # 5. the "uses X on 自分/self" nicety.
    assert fn("ホイミを　じぶんに自分", "<%sM_00>") == "ホイミを　じぶんにself"
    # 6. a whitelisted GENERIC string -> the text path -> MT (sync) -> normalized.
    out = fn("やまの　むこう。", "<%sM_00>")
    assert out is not None and "-EN" in out                # machine-translated via the text path
    c.close()


def test_dispatch_network_kaisetubun_uses_narrow_wrap(tmp_path):
    """The Story So Far recap (<%sM_kaisetubun>) wraps at the narrow panel width (no <br>).

    Built with the real network_text profile (lines_per_page=0) — the panel renders <br> literally
    and is only ~9 lines tall, so the recap path must NOT paginate and must wrap at the narrower
    KAISETUBUN_WRAP (38) rather than the 46-col dialogue width.
    """
    c = TranslationCache(tmp_path / "c.db")
    long_en = (
        "The hero set out from the village at dawn and travelled across the windswept plains, "
        "through dark forests and over high mountain passes, seeking the lost relic of legend."
    )
    sync = StubProvider("googletranslatefree", lambda t: long_en)
    t = Translator(c, sync_provider=sync)
    fn = build_network_translate_fn(_cfg(), t, lines_per_page=0, sync=True)
    out = fn("ものがたりの　あらすじ。", "<%sM_kaisetubun>")
    assert out is not None
    assert "<br>" not in out                               # the panel doesn't paginate on <br>
    # wrapped to the NARROWER kaisetubun width (38), not the 46-col dialogue width.
    from dqxclarity.runtime.dispatch import KAISETUBUN_WRAP

    assert all(len(line) <= KAISETUBUN_WRAP for line in out.split("\n") if line)
    c.close()


def test_dispatch_runtime_name_update_is_picked_up_without_rebuild(tmp_path):
    """The community lookup reads names LIVE from the translator (player hook updates apply at once).

    _make_community_lookup must NOT capture the names at build time: mutating the translator's
    player_name_* after building the fn must change the very next lookup's result.
    """
    c = TranslationCache(tmp_path / "c.db")
    c.store("<pnplacehold>の　ぼうけん", "<pnplacehold>'s adventure", "community")
    t = Translator(c)
    # cfg has no names; build the fn, THEN set the names on the translator (as the player hook does).
    fn, _ = build_translate_fn(_cfg(), t, sync=True)
    t.player_name_ja = "タイカン"
    t.player_name_en = "Taikan"
    assert fn("タイカンの　ぼうけん") == "Taikan's adventure"
    # Swap to a different player at runtime — the next lookup uses the NEW name with no rebuild.
    t.player_name_ja = "ベスト"
    t.player_name_en = "Best"
    assert fn("ベストの　ぼうけん") == "Best's adventure"
    c.close()
