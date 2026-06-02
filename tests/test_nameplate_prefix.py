"""Tests for the nameplates \x04 overhead-name prefix (audit gap #25).

Upstream app/hooking/hooks/nameplates.py:54 returns ``"\x04" + result`` for every nameplate
replacement; without the \x04 a replaced overhead name renders RED with a GM-avatar chat picture
(upstream comment lines 50-53). Our nameplates surface uses build_name_translate_fn, which dropped
the prefix. The fix adds an optional ``prefix`` kwarg to build_name_translate_fn that is prepended
to the WRITTEN value (never the lookup key) ONLY when a real replacement is produced — never to a
pass-through None.

CRITICAL nuance: the \x04 prefix is for the NAMEPLATES hook ONLY. Upstream does NOT prefix the
network_text name categories (which also route through the name path), so build_network_translate_fn
must keep prefix="" and produce NO \x04. These tests lock both halves in.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dqxclarity.runtime.dispatch import (
    build_name_translate_fn,
    build_network_translate_fn,
)
from dqxclarity.translate.db import TranslationCache
from dqxclarity.translate.pipeline import Translator
from dqxclarity.translate.romanize import is_available


def _cfg(**over):
    tr = SimpleNamespace(
        player_name_ja="", player_name_en="", sibling_name_ja="", sibling_name_en="",
        wrap_width=46, lines_per_page=0, battle_names=True,
    )
    for k, v in over.items():
        setattr(tr, k, v)
    return SimpleNamespace(translate=tr)


# ------------------------------------------------------------------ prefix on a community/cache hit


def test_prefix_prepended_to_community_hit(tmp_path):
    # A curated NPC/monster name resolves via the community/cache hit; the \x04 prefix must be
    # prepended to the WRITTEN value (so the overhead name doesn't render red).
    c = TranslationCache(tmp_path / "p1.db")
    c.store("スライム", "Slime", "community")
    t = Translator(c)
    fn = build_name_translate_fn(_cfg(), t, prefix="\x04")
    assert fn("スライム") == "\x04Slime"


def test_prefix_is_on_value_not_lookup_key(tmp_path):
    # The prefix must be added to the OUTPUT, not used as part of the cache key. The DB is keyed on
    # the bare Japanese ("スライム"); a hit still resolves, proving the lookup key carries no \x04.
    c = TranslationCache(tmp_path / "p2.db")
    c.store("スライム", "Slime", "community")
    t = Translator(c)
    fn = build_name_translate_fn(_cfg(), t, prefix="\x04")
    out = fn("スライム")
    assert out == "\x04Slime"
    # The bare key is what's stored; a prefixed key would NOT be present.
    assert t.lookup("スライム") == "Slime"
    # A miss returns None (the prefix is never folded into the lookup key).
    assert t.lookup("\x04スライム") is None


# ------------------------------------------------------------------------ prefix on a romanized name


def test_prefix_prepended_to_romanized_name(tmp_path):
    # An uncached (player-style) name has no community entry, so it romanizes — the prefix must be
    # prepended to the romaji too. Skips cleanly if pykakasi isn't installed.
    if not is_available():
        pytest.skip("pykakasi unavailable")
    c = TranslationCache(tmp_path / "p3.db")
    t = Translator(c)
    fn = build_name_translate_fn(_cfg(), t, prefix="\x04")
    out = fn("たろう")
    assert out is not None
    assert out.startswith("\x04")
    assert out[1:].lower().startswith("tar")  # \x04Tarou


# ----------------------------------------------------------------- NEVER prefix a pass-through None


def test_prefix_not_added_to_non_japanese_passthrough(tmp_path):
    # Non-Japanese input is a pass-through (leave as-is) -> None, NEVER "\x04" alone.
    c = TranslationCache(tmp_path / "p4.db")
    t = Translator(c)
    fn = build_name_translate_fn(_cfg(), t, prefix="\x04")
    assert fn("Bob") is None


def test_prefix_not_added_when_no_hit_and_no_romanizer(tmp_path, monkeypatch):
    # A Japanese name with no community hit AND no romanizer is a pass-through -> None, NEVER a bare
    # "\x04" prefix on a None. Force romanize unavailable so we exercise the no-romanizer branch.
    import dqxclarity.runtime.dispatch as dispatch

    monkeypatch.setattr(dispatch.romanize, "is_available", lambda: False)
    c = TranslationCache(tmp_path / "p5.db")
    t = Translator(c)
    fn = build_name_translate_fn(_cfg(), t, prefix="\x04")
    out = fn("だれもしらないなまえ")  # uncached, no romanizer
    assert out is None  # pass-through None, not "\x04" and not "\x04None"


# ------------------------------------------------------------------ default prefix="" is unchanged


def test_default_prefix_empty_leaves_hit_unchanged(tmp_path):
    # REGRESSION: the default (no prefix kwarg) must NOT prepend anything — existing callers keep
    # the exact prior behaviour.
    c = TranslationCache(tmp_path / "p6.db")
    c.store("スライム", "Slime", "community")
    t = Translator(c)
    fn = build_name_translate_fn(_cfg(), t)
    assert fn("スライム") == "Slime"
    assert "\x04" not in (fn("スライム") or "")


def test_default_prefix_empty_leaves_romaji_and_none_unchanged(tmp_path):
    # REGRESSION: default prefix leaves romaji unprefixed and pass-throughs as None.
    c = TranslationCache(tmp_path / "p7.db")
    t = Translator(c)
    fn = build_name_translate_fn(_cfg(), t)
    assert fn("Bob") is None  # non-Japanese pass-through
    if is_available():
        out = fn("たろう")
        assert out is not None and not out.startswith("\x04")


# -------------------------------------------------- network_text name categories produce NO \x04


def test_network_text_name_category_has_no_prefix(tmp_path):
    # CRITICAL nuance: upstream does NOT prefix network_text name categories. A name-category hit
    # routed through build_network_translate_fn must carry NO \x04 (build_network_translate_fn builds
    # its name path with the default prefix="").
    c = TranslationCache(tmp_path / "p8.db")
    c.store("スライム", "Slime", "community")
    t = Translator(c)
    fn = build_network_translate_fn(_cfg(), t)
    out = fn("スライム", "<%sM_npc>")
    assert out == "Slime"
    assert "\x04" not in out


def test_network_text_name_category_romaji_has_no_prefix(tmp_path):
    # Even the uncached romanized path through a network_text NAME category must carry NO \x04.
    if not is_available():
        pytest.skip("pykakasi unavailable")
    c = TranslationCache(tmp_path / "p9.db")
    t = Translator(c)
    fn = build_network_translate_fn(_cfg(), t)
    out = fn("たろう", "<%sM_pc>")
    assert out is not None
    assert not out.startswith("\x04")
    assert "\x04" not in out
