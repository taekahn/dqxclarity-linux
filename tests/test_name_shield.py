"""Tests for player/sibling proper-name shielding on the MT path (GAP #25).

The player's/sibling's LITERAL Japanese name appears inline in ordinary game text
(e.g. "タイカンは3600ゴールドを手に入れた！" = "<name> received 3600 Gold!"). On the MT path the bare JA
name is corrupted two ways:
  1. the GLOSSARY does naive substring substitution — a term イカ -> "Squid" matches INSIDE the
     name タ-イカ-ン, so glossify('タイカン') becomes 'タ Squid ン' -> "Squid Tan" after MT.
  2. the machine translator mangles the bare name itself (タイカン -> "Taycan"/"Tycoon").

Translator._shield_names swaps each literal JA name for the correct EN name wrapped in an MT-proof
sentinel (tags.shield_name) BEFORE glossify/MT, so neither can touch it; restore_tags un-wraps it.
This covers BOTH MT call sites (the sync translate_now and the background _run worker) since they
share Translator._prepare_for_mt.

Mirrors the honorific tests in tests/test_mt_output_polish.py (the _StubProvider that records what
it was asked to translate). Offline only — no network, no game.
"""

from __future__ import annotations

import time

from dqxclarity.translate.db import TranslationCache
from dqxclarity.translate.glossary import Glossary
from dqxclarity.translate.pipeline import Translator


class _EchoProvider:
    """Records what it was asked to translate and ECHOES it back unchanged.

    Echoing lets a test inspect what actually reached MT (the .seen list) AND assert on the final
    rendered output (the echoed-then-restored text) at the same time.
    """

    name = "googletranslatefree"  # rank 1 (a real provider name so ranking works)

    def __init__(self) -> None:
        self.seen: list[str] = []

    def available(self) -> bool:
        return True

    def translate(self, texts):
        self.seen.extend(texts)
        return list(texts)


class _NameManglingProvider:
    """A provider that corrupts the bare JA name if it ever sees it (models real MT mangling).

    Returns "Taycan" for any input still containing the literal JA name タイカン; otherwise echoes.
    With the shield in place the provider never sees タイカン, so the EN name survives intact.
    """

    name = "googletranslatefree"

    def __init__(self) -> None:
        self.seen: list[str] = []

    def available(self) -> bool:
        return True

    def translate(self, texts):
        self.seen.extend(texts)
        out = []
        for t in texts:
            out.append("Taycan" if "タイカン" in t else t)
        return out


# --- the headline GLOSSARY-corruption case --------------------------------------------------- #


def test_glossary_does_not_corrupt_player_name(tmp_path):
    # Glossary term イカ -> "Squid" would substring-match INSIDE タ-イカ-ン without the shield.
    glossary = Glossary([("イカ", "Squid")])
    prov = _EchoProvider()
    c = TranslationCache(tmp_path / "g.db")
    t = Translator(c, sync_provider=prov, glossary=glossary)
    t.player_name_ja = "タイカン"
    t.player_name_en = "Taikan"

    out = t.translate_now("タイカンは3600ゴールドを手に入れた！")

    # What actually reached MT must NOT be the glossary-mangled "タ Squid ン" or the raw JA name.
    assert prov.seen, "provider should have been called"
    sent = prov.seen[0]
    assert "Squid" not in sent  # the glossary never substring-matched inside the shielded name
    assert "タイカン" not in sent  # the raw JA name never reached glossify/MT
    assert "Taikan" in sent  # the EN name (behind the sentinel) is what the glossary/MT saw

    # The final rendered output has the correct EN name and none of the corruption.
    assert out is not None
    assert "Taikan" in out
    assert "Squid" not in out
    assert "Tan" not in out
    assert "タ" not in out
    c.close()


# --- the MT-mangle case ----------------------------------------------------------------------- #


def test_mt_does_not_mangle_shielded_player_name(tmp_path):
    prov = _NameManglingProvider()  # would return "Taycan" if it saw タイカン
    c = TranslationCache(tmp_path / "m.db")
    t = Translator(c, sync_provider=prov)
    t.player_name_ja = "タイカン"
    t.player_name_en = "Taikan"

    out = t.translate_now("タイカンは3600ゴールドを手に入れた！")

    # The provider never saw the bare JA name, so its mangling rule never fired.
    assert "タイカン" not in prov.seen[0]
    assert out is not None
    assert "Taikan" in out
    assert "Taycan" not in out
    c.close()


# --- sibling name shielded the same way ------------------------------------------------------- #


def test_sibling_name_is_shielded(tmp_path):
    glossary = Glossary([("イカ", "Squid")])
    prov = _EchoProvider()
    c = TranslationCache(tmp_path / "s.db")
    t = Translator(c, sync_provider=prov, glossary=glossary)
    t.sibling_name_ja = "タイカン"
    t.sibling_name_en = "Taikan"

    out = t.translate_now("タイカンは3600ゴールドを手に入れた！")

    assert "Squid" not in prov.seen[0]
    assert "タイカン" not in prov.seen[0]
    assert out is not None
    assert "Taikan" in out
    assert "Squid" not in out
    c.close()


def test_both_names_shielded_longer_first(tmp_path):
    # When one name is a substring of the other, the LONGER name must be shielded whole first so the
    # shorter one can't partial-replace inside it. Player "タイカン" contains sibling "カン" only as a
    # mid-word tail, which the boundary lookbehind also rejects — so the standalone sibling name in
    # its own clause is what gets shielded, and the player name is shielded whole.
    prov = _EchoProvider()
    c = TranslationCache(tmp_path / "b.db")
    t = Translator(c, sync_provider=prov)
    t.player_name_ja = "タイカン"
    t.player_name_en = "Taikan"
    t.sibling_name_ja = "カン"
    t.sibling_name_en = "Kan"

    out = t.translate_now("タイカン")  # standalone player name only
    assert out is not None
    # The whole player name resolved to Taikan, NOT a partial "TaiKan"-style mangle from カン.
    assert out == "Taikan"
    c.close()


# --- empty-names no-op ------------------------------------------------------------------------ #


def test_shield_names_noop_when_names_empty(tmp_path):
    c = TranslationCache(tmp_path / "e.db")
    t = Translator(c)  # both names default to ""
    s = "タイカンは3600ゴールドを手に入れた！"
    assert t._shield_names(s) == s  # untouched: no known name to anchor on
    c.close()


def test_shield_names_direct_replaces_all_occurrences(tmp_path):
    # _shield_names replaces EVERY standalone occurrence of the name with the EN-name sentinel.
    from dqxclarity.translate.tags import restore_tags, shield_name

    c = TranslationCache(tmp_path / "d.db")
    t = Translator(c)
    t.player_name_ja = "タイカン"
    t.player_name_en = "Taikan"
    shielded = t._shield_names("タイカンと タイカン")
    assert "タイカン" not in shielded
    assert shielded == f"{shield_name('Taikan')}と {shield_name('Taikan')}"
    # And restore_tags un-wraps the sentinel back to the plain EN name.
    assert restore_tags(shielded) == "Taikanと Taikan"
    c.close()


def test_shield_names_boundary_safe_midword(tmp_path):
    # A name that is merely the TAIL of a longer Japanese word must NOT be shielded mid-word
    # (mirrors _strip_name_honorifics' boundary rule). sibling name "カン" inside "タイカン" is
    # mid-word, so it stays untouched.
    c = TranslationCache(tmp_path / "mw.db")
    t = Translator(c)
    t.sibling_name_ja = "カン"
    t.sibling_name_en = "Kan"
    # 'カン' here is the tail of 'タイカン' (preceded by 'イ', a katakana char) -> not shielded.
    assert t._shield_names("タイカン") == "タイカン"
    c.close()


# --- background (_run) path also shields ------------------------------------------------------ #


def test_background_run_shields_player_name(tmp_path):
    # The background _run (upgrade) worker must shield the name before the slow provider sees it,
    # exactly like the sync path — both share _prepare_for_mt. Seed a rank-1 entry so the JA is a
    # cache hit (no sync MT), then drive the rank-2 upgrade worker and inspect what it received.
    glossary = Glossary([("イカ", "Squid")])
    c = TranslationCache(tmp_path / "u.db")
    ja = "タイカンは3600ゴールドを手に入れた！"
    c.store(ja, "placeholder", "googletranslatefree")  # rank 1 -> upgradeable

    upgrade = _EchoProvider()
    upgrade.name = "claude_cli"  # rank 2, so it upgrades the rank-1 entry
    t = Translator(c, upgrade_provider=upgrade, glossary=glossary)
    t.player_name_ja = "タイカン"
    t.player_name_en = "Taikan"
    t.start()
    try:
        t.request_upgrade(ja)
        deadline = time.time() + 3
        while time.time() < deadline and not upgrade.seen:
            time.sleep(0.02)
    finally:
        t.stop()

    assert upgrade.seen, "upgrade provider should have been called"
    sent = upgrade.seen[0]
    assert "Squid" not in sent  # glossary never matched inside the shielded name
    assert "タイカン" not in sent  # raw JA name never reached the slow provider
    assert "Taikan" in sent
    # And the cache now holds the restored EN name (not the corruption).
    final = c.lookup(ja)
    assert final is not None and "Taikan" in final and "Squid" not in final
    c.close()


# --- still-strips-honorific-then-shields interplay -------------------------------------------- #


def test_honorific_stripped_then_name_shielded(tmp_path):
    # The honorific is stripped FIRST (exposing the bare name), then the bare name is shielded.
    prov = _EchoProvider()
    c = TranslationCache(tmp_path / "h.db")
    t = Translator(c, sync_provider=prov)
    t.player_name_ja = "タイカン"
    t.player_name_en = "Taikan"

    out = t.translate_now("タイカンさま")  # name + honorific
    assert prov.seen
    assert "さま" not in prov.seen[0]  # honorific stripped
    assert "タイカン" not in prov.seen[0]  # bare name shielded
    assert "Taikan" in prov.seen[0]
    assert out is not None and "Taikan" in out
    c.close()


def test_leading_prefix_sibling_longer_first_no_corruption(tmp_path):
    """The sort is load-bearing when the sibling name is a LEADING PREFIX of the player name:
    カン (Kan) starts カンタ (Kanta). The lookbehind does NOT fire on カン at string start, so without
    longer-name-first the shorter カン would corrupt カンタ -> 'Kanタ'. Longer-first shields it whole."""
    prov = _EchoProvider()
    c = TranslationCache(tmp_path / "prefix.db")
    t = Translator(c, sync_provider=prov)
    t.player_name_ja, t.player_name_en = "カンタ", "Kanta"
    t.sibling_name_ja, t.sibling_name_en = "カン", "Kan"

    out = t.translate_now("カンタ")
    sent = prov.seen[0]
    assert "カン" not in sent and "カンタ" not in sent  # shielded whole, not split into カン + タ
    assert out == "Kanta"
    c.close()
