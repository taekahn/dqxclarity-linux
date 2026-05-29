"""Tests for MT-output / MT-input polish in the Translator (GAP #22 and GAP #24-honorific).

These cover the machine-translation path ONLY (Translator.translate_now's sync provider and
Translator._run's upgrade provider). Community/cache hits are served by ``lookup`` and must never be
normalized. Upstream references: __normalize_text (translate.py:54-60) and the honorific strip
(translate.py:316-321).
"""

from __future__ import annotations

import time

from dqxclarity.translate.db import TranslationCache
from dqxclarity.translate.pipeline import Translator


class _StubProvider:
    """Records what it was asked to translate and returns a fixed canned output."""

    name = "googletranslatefree"  # rank 1 (a real provider name so ranking works)

    def __init__(self, output):
        self._output = output
        self.seen: list[str] = []

    def available(self):
        return True

    def translate(self, texts):
        self.seen.extend(texts)
        return [self._output for _ in texts]


# ----- GAP #22: _normalize_mt_output --------------------------------------- #


def test_normalize_mt_output_curly_apostrophe():
    # Both curly single-quote variants fold to a straight apostrophe.
    assert Translator._normalize_mt_output("It’s") == "It's"
    assert Translator._normalize_mt_output("‘quoted’") == "'quoted'"


def test_normalize_mt_output_curly_quotes():
    assert Translator._normalize_mt_output("“hi”") == '"hi"'


def test_normalize_mt_output_dashes():
    # Em-dash and en-dash become a plain hyphen (NOT deleted).
    assert Translator._normalize_mt_output("a — b") == "a - b"
    assert Translator._normalize_mt_output("a – b") == "a - b"


def test_normalize_mt_output_ellipsis():
    # Single-char ellipsis expands to three ASCII dots.
    assert Translator._normalize_mt_output("wait…") == "wait..."


def test_normalize_mt_output_accents():
    # NFKD fold strips accents to ASCII.
    assert Translator._normalize_mt_output("café") == "cafe"
    assert Translator._normalize_mt_output("naïve résumé") == "naive resume"


def test_normalize_mt_output_combined():
    # The exact provider string used by the translate_now test.
    assert (
        Translator._normalize_mt_output("It’s a café — really…")
        == "It's a cafe - really..."
    )


def test_normalize_mt_output_plain_ascii_unchanged():
    s = "Just plain ASCII text 123 - ok."
    assert Translator._normalize_mt_output(s) == s


def test_normalize_mt_output_dash_not_dropped_before_fold():
    # Regression: the targeted replacement must run BEFORE the NFKD ascii-ignore fold, otherwise
    # the em-dash would be deleted instead of turned into '-'.
    out = Translator._normalize_mt_output("end—")
    assert out == "end-"
    assert "-" in out  # dash survived as ASCII, was not dropped


# ----- GAP #22 applied to the MT path -------------------------------------- #


def test_translate_now_normalizes_provider_output(tmp_path):
    c = TranslationCache(tmp_path / "c.db")
    prov = _StubProvider("It’s a café — really…")
    t = Translator(c, sync_provider=prov)
    out = t.translate_now("こんにちは")  # こんにちは (a cache miss -> MT)
    assert out == "It's a cafe - really..."
    # And the normalized form is what gets cached.
    assert c.lookup("こんにちは") == "It's a cafe - really..."
    c.close()


def test_community_cache_hit_is_not_normalized(tmp_path):
    c = TranslationCache(tmp_path / "h.db")
    # A community hit that legitimately contains a curly apostrophe is served unchanged.
    ja = "ありがとう"  # ありがとう
    c.store(ja, "It’s done", "community")
    prov = _StubProvider("SHOULD NOT BE CALLED")
    t = Translator(c, sync_provider=prov)
    out = t.translate_now(ja)
    assert out == "It’s done"  # curly apostrophe preserved, NOT normalized
    assert prov.seen == []  # provider never invoked for a cache hit
    c.close()


# ----- GAP #24-honorific: _strip_name_honorifics --------------------------- #


def test_strip_name_honorifics_after_player_name(tmp_path):
    c = TranslationCache(tmp_path / "n.db")
    t = Translator(c)
    t.player_name_ja = "タイカン"  # タイカン
    # タイカンさま -> タイカン
    assert (
        t._strip_name_honorifics("タイカンさま")
        == "タイカン"
    )
    c.close()


def test_strip_name_honorifics_leaves_unrelated_san_word(tmp_path):
    c = TranslationCache(tmp_path / "n.db")
    t = Translator(c)
    t.player_name_ja = "タイカン"  # タイカン
    # おじいさん contains さん but is NOT the name -> untouched.
    word = "おじいさん"  # おじいさん
    assert t._strip_name_honorifics(word) == word
    c.close()


def test_strip_name_honorifics_does_not_strip_name_as_word_suffix(tmp_path):
    # Regression: the name must be a STANDALONE occurrence, never the TAIL of a longer JA word.
    # player_name_ja='カン' (カン). In 'タイカンさんは強い' (タイカンさんは強い), 'カン' is mid-word
    # (the end of タイカン), so the trailing さん must NOT be stripped. A bare str.replace would
    # over-strip here; the word-boundary regex (negative lookbehind on JA chars) must leave it.
    c = TranslationCache(tmp_path / "w.db")
    t = Translator(c)
    t.player_name_ja = "カン"  # カン
    assert (
        t._strip_name_honorifics("タイカンさんは強い")  # タイカンさんは強い
        == "タイカンさんは強い"  # unchanged
    )
    c.close()


def test_strip_name_honorifics_sibling_name(tmp_path):
    c = TranslationCache(tmp_path / "n.db")
    t = Translator(c)
    t.sibling_name_ja = "ミレイユ"  # ミレイユ
    # ミレイユちゃん -> ミレイユ
    assert (
        t._strip_name_honorifics("ミレイユちゃん")
        == "ミレイユ"
    )
    c.close()


def test_strip_name_honorifics_noop_when_names_empty(tmp_path):
    c = TranslationCache(tmp_path / "n.db")
    t = Translator(c)  # both names default to ""
    # A string that ends in さん is left alone because there is no known name to anchor on.
    s = "タイカンさま"  # タイカンさま
    assert t._strip_name_honorifics(s) == s
    c.close()


def test_translate_now_strips_honorific_before_provider(tmp_path):
    c = TranslationCache(tmp_path / "p.db")
    prov = _StubProvider("Taikan")
    t = Translator(c, sync_provider=prov)
    t.player_name_ja = "タイカン"  # タイカン
    t.translate_now("タイカンさま")  # タイカンさま (cache miss -> MT)
    # The honorific was stripped from the JA the provider actually received.
    assert prov.seen == ["タイカン"]  # タイカン (no さま)
    c.close()


def test_run_strips_honorific_before_upgrade_provider(tmp_path):
    # The background _run (upgrade) path must also strip the honorific before the slow provider
    # sees the JA — mirroring the sync path. Seed a rank-1 entry for the JA-with-honorific so it's a
    # cache hit (no sync MT), then drive the rank-2 upgrade worker and inspect what it received.
    c = TranslationCache(tmp_path / "u.db")
    ja = "タイカンさま"  # タイカンさま (player name + honorific)
    c.store(ja, "Lord Taikan", "googletranslatefree")  # rank 1 -> upgradeable
    upgrade = _StubProvider("Taikan")
    upgrade.name = "claude_cli"  # rank 2, so it upgrades the rank-1 entry
    t = Translator(c, upgrade_provider=upgrade)
    t.player_name_ja = "タイカン"  # タイカン
    t.start()
    try:
        t.request_upgrade(ja)  # queue the JA-with-honorific for background re-translation
        deadline = time.time() + 3
        while time.time() < deadline and not upgrade.seen:
            time.sleep(0.02)
    finally:
        t.stop()
    # The honorific was stripped from the JA the upgrade provider actually received.
    assert upgrade.seen == ["タイカン"]  # タイカン (no さま)
    c.close()
