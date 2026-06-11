"""Tests for the NOVEL battle monster/actor NAME surface (no upstream equivalent).

The network_text battle surface hands us full templates whose captured argument is a Japanese
monster/actor name, e.g. ``\\sしびれくらげ\\mしびれくらげ\\e takes <%dB_VALUE> damage!`` (the
``\\s`` ``\\m`` ``\\e`` markers wrap the name + an internal id). ``_translate_name_runs`` name-ifies
each Japanese run in place — player/sibling substitution FIRST (so the ``タイカン`` player/monster
collision resolves to the player, NOT the cached monster "Squid"), then cache/community, else offline
romaji. NO machine translation anywhere on this path (instant, hot-path safe).

``build_network_translate_fn``'s battle branch routes any category CONTAINING a battle name-tag to
this pass when ``cfg.translate.battle_names`` is on, and is skipped (old drop behaviour) when off.

All offline: a fake translator backs ``lookup``/``translate_name`` with a small dict + a romaji-ish
fallback for misses; no game, no network, no provider.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dqxclarity.config import Config, TranslateConfig
from dqxclarity.runtime.dispatch import (
    BATTLE_NAME_TAGS,
    _translate_name_runs,
    build_network_translate_fn,
)
from dqxclarity.translate.db import TranslationCache
from dqxclarity.translate.pipeline import Translator

# Community/cache-resolved names. NOTE the trap: タイカン exists BOTH as the player ("Taikan") and as
# a cached monster ("Squid") — player substitution must win.
_NAMES = {
    "しびれくらげ": "Man o' War",
    "キメラ": "Chimaera",
    "ジャガーメイジ": "Clawcerer",
    "タイカン": "Squid",  # monster collision with the player name
}


def _fallback_romaji(ja: str) -> str:
    """Deterministic stand-in for offline romanization of an uncached name (e.g. ツナーム)."""
    return "romaji(" + ja + ")"


class FakeTranslator:
    """Minimal translator exposing exactly what _translate_name_runs reads.

    ``lookup`` is the in-memory cache hit; ``translate_name`` is cache-hit-else-romaji (NEVER MT),
    matching pipeline.Translator.translate_name's contract.
    """

    def __init__(self, *, player_ja="タイカン", player_en="Taikan", sibling_ja="", sibling_en=""):
        self.player_name_ja = player_ja
        self.player_name_en = player_en
        self.sibling_name_ja = sibling_ja
        self.sibling_name_en = sibling_en
        self.mt_called = False  # tripped if any MT-ish path were ever invoked (it must not be)

    def lookup(self, ja: str) -> str | None:
        return _NAMES.get(ja)

    def translate_name(self, ja: str) -> str:
        hit = _NAMES.get(ja)
        return hit if hit is not None else _fallback_romaji(ja)


def _net_cfg(*, battle_names=True, player_ja="タイカン", player_en="Taikan"):
    """A cfg whose .translate carries battle_names + live names (SimpleNamespace, no disk).

    Pins network_translate_all=False so these tests exercise the LEGACY whitelist path, where the
    ``battle_names`` toggle is the gate for the name-ify pass. (Under the new translate_all default,
    a battle template name-ifies via NAME_TAGS regardless of battle_names — covered in
    test_network_text.py::test_translate_all_battle_template_is_name_ified.)
    """
    tr = SimpleNamespace(
        player_name_ja=player_ja, player_name_en=player_en,
        sibling_name_ja="", sibling_name_en="",
        wrap_width=46, lines_per_page=0, battle_names=battle_names,
        network_translate_all=False,
    )
    return SimpleNamespace(translate=tr)


# --------------------------------------------------------------- _translate_name_runs (the core pass)


def test_markers_preserved_and_both_names_resolved():
    # \s name \m name (\e absent here) — markers/structure verbatim, both JA runs -> "Man o' War".
    t = FakeTranslator()
    out = _translate_name_runs("\\sしびれくらげ\\mしびれくらげ", t)
    assert out == "\\sMan o' War\\mMan o' War"


def test_player_collision_resolves_to_player_not_monster():
    # HEADLINE CORRECTNESS CASE: タイカン is BOTH the player ("Taikan") and a cached monster ("Squid").
    # Player substitution must win -> "Taikan", never "Squid".
    t = FakeTranslator(player_ja="タイカン", player_en="Taikan")
    assert _translate_name_runs("タイカン", t) == "Taikan"
    # Sanity: the cache really would have said "Squid" without the player override.
    assert t.lookup("タイカン") == "Squid"


def test_player_collision_with_empty_player_en_falls_back_to_translate_name():
    # If the player has no EN name yet, fall through to translate_name (which would be the cache hit).
    t = FakeTranslator(player_ja="タイカン", player_en="")
    assert _translate_name_runs("タイカン", t) == "Squid"


def test_sibling_substitution_takes_precedence_over_cache():
    t = FakeTranslator(player_ja="", player_en="", sibling_ja="しびれくらげ", sibling_en="MyBro")
    # The sibling JA happens to collide with a cached monster; sibling EN must win.
    assert _translate_name_runs("しびれくらげ", t) == "MyBro"


def test_full_rendered_message_names_ified_in_order():
    # A fully-rendered battle line: player + ASCII verb + monster, all preserved/resolved in order.
    t = FakeTranslator()
    assert _translate_name_runs("タイカン defeated しびれくらげ", t) == "Taikan defeated Man o' War"


def test_miss_falls_back_to_romaji_not_crash():
    t = FakeTranslator()
    # ツナーム is in neither the player slot nor the cache -> offline romaji fallback, no crash.
    assert _translate_name_runs("ツナーム", t) == _fallback_romaji("ツナーム")


def test_pure_non_japanese_returns_none():
    t = FakeTranslator()
    assert _translate_name_runs("\\s12345", t) is None
    assert _translate_name_runs("Lv 26", t) is None
    assert _translate_name_runs("\\s\\m\\e takes 42 damage!", t) is None


def test_template_with_unresolved_param_tag_and_a_name():
    # A real battle template shape: markers + name + an unrendered numeric param tag. The name run
    # resolves; everything non-Japanese (markers, the <%dB_VALUE> tag, ASCII words) is verbatim.
    t = FakeTranslator()
    out = _translate_name_runs("\\sキメラ\\mキメラ\\e takes <%dB_VALUE> damage!", t)
    assert out == "\\sChimaera\\mChimaera\\e takes <%dB_VALUE> damage!"


# NOTE: the "no machine translation" property is enforced end-to-end by the routing tests below,
# which build a real Translator wired to a LoudProvider that RAISES if any MT call fires.

# ------------------------------------------------------ routing via build_network_translate_fn
#
# build_network_translate_fn builds inner text/name fns at construction (which read a real
# Translator's interface), so the routing tests use a real Translator + cache seeded from _NAMES.
# The cache holds タイカン->"Squid" (the monster); the live player name on the translator must
# override it to "Taikan" in the battle path.


class LoudProvider:
    """Trips ``called`` if ANY MT fires — the battle name path must never invoke it."""

    name = "googletranslatefree"
    called = False

    def available(self):
        return True

    def translate(self, texts):
        LoudProvider.called = True
        return ["SHOULD-NOT-APPEAR" for _ in texts]


def _real_translator(tmp_path, name):
    c = TranslationCache(tmp_path / name)
    for ja, en in _NAMES.items():
        c.store(ja, en, "community")
    t = Translator(c, sync_provider=LoudProvider())
    t.player_name_ja = "タイカン"
    t.player_name_en = "Taikan"
    return t


def test_battle_template_category_is_name_ified(tmp_path):
    LoudProvider.called = False
    t = _real_translator(tmp_path, "bt1.db")
    fn = build_network_translate_fn(_net_cfg(), t)
    category = "\\s<%sB_TARGET>\\m<%sB_TARGET_ID>\\e takes <%dB_VALUE> damage!"
    assert any(tag in category for tag in BATTLE_NAME_TAGS)  # the category carries a battle name tag
    out = fn("\\sしびれくらげ\\mしびれくらげ\\e", category)
    assert out == "\\sMan o' War\\mMan o' War\\e"
    assert LoudProvider.called is False  # NO machine translation on the battle path


def test_battle_template_player_collision_routed_to_player(tmp_path):
    # End-to-end through the router: the player name in a battle template resolves to the player,
    # NOT the cached monster "Squid".
    LoudProvider.called = False
    t = _real_translator(tmp_path, "bt2.db")
    fn = build_network_translate_fn(_net_cfg(), t)
    category = "\\s<%sB_ACTOR>\\m<%sB_ACTOR_ID>\\e"
    out = fn("\\sタイカン\\mタイカン\\e", category)
    assert out == "\\sTaikan\\mTaikan\\e"  # NOT "Squid"
    assert LoudProvider.called is False


def test_number_only_battle_template_is_untouched(tmp_path):
    # A category with NO battle name tag (only a numeric value tag) is not routed to the name pass.
    t = _real_translator(tmp_path, "bt3.db")
    fn = build_network_translate_fn(_net_cfg(), t)
    assert not any(tag in "<%dM_00>" for tag in BATTLE_NAME_TAGS)
    assert fn("12345", "<%dM_00>") is None  # non-Japanese digits anyway -> None


def test_battle_branch_skipped_when_toggle_off(tmp_path):
    # With battle_names False the branch is skipped entirely -> old behaviour (drop / pass through).
    t = _real_translator(tmp_path, "bt4.db")
    fn = build_network_translate_fn(_net_cfg(battle_names=False), t)
    category = "\\s<%sB_TARGET>\\m<%sB_TARGET_ID>\\e takes <%dB_VALUE> damage!"
    assert fn("\\sしびれくらげ\\mしびれくらげ\\e", category) is None


def test_battle_tags_set_contents():
    assert BATTLE_NAME_TAGS == frozenset({"<%sB_ACTOR>", "<%sB_TARGET>", "<%sB_TARGET2>"})


# ----------------------------------------------------------------------- config round-trip


def test_battle_names_default_true():
    assert TranslateConfig().battle_names is True
    assert Config().translate.battle_names is True


def test_battle_names_config_round_trip(tmp_path, monkeypatch):
    from dqxclarity import config as cfg_mod

    d = tmp_path / "cfgdir"
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", d)
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", d / "config.toml")

    c = Config()
    c.translate.battle_names = False
    cfg_mod.save(c)
    back = cfg_mod.load()
    assert back.translate.battle_names is False and isinstance(back.translate.battle_names, bool)

    c.translate.battle_names = True
    cfg_mod.save(c)
    assert cfg_mod.load().translate.battle_names is True


def test_battle_names_config_set_round_trip(tmp_path, monkeypatch):
    from dqxclarity import config as cfg_mod
    from dqxclarity.cli import config_set

    d = tmp_path / "cfgdir2"
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", d)
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", d / "config.toml")

    config_set("translate.battle_names", "false")
    assert cfg_mod.load().translate.battle_names is False
    config_set("translate.battle_names", "true")
    assert cfg_mod.load().translate.battle_names is True
