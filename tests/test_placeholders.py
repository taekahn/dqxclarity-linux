"""Player-name placeholder conventions: token parameterization + the dual-convention community
lookup that lets the quest/event/system corpus (``<pc>``/``<kyodai>``) hit alongside the dialogue
corpus (``<pnplacehold>``/``<snplacehold>``). Nothing here touches the game or network.
"""

from __future__ import annotations

from types import SimpleNamespace

from dqxclarity.runtime.dispatch import _make_community_lookup
from dqxclarity.translate.db import TranslationCache
from dqxclarity.translate.pipeline import Translator
from dqxclarity.translate.placeholders import from_placeholders, to_placeholders


def _cfg(**over):
    tr = SimpleNamespace(
        player_name_ja="", player_name_en="", sibling_name_ja="", sibling_name_en="",
        wrap_width=46, lines_per_page=3,
    )
    for k, v in over.items():
        setattr(tr, k, v)
    return SimpleNamespace(translate=tr)


# --------------------------------------------------------------------------- token parameterization


def test_to_from_placeholders_pc_convention_round_trip():
    """With pn=<pc>/sn=<kyodai>, a literal name round-trips through the placeholder."""
    ja = "タイカンは アイテムを つかった。"
    key = to_placeholders(ja, "タイカン", "", pn="<pc>", sn="<kyodai>")
    assert key == "<pc>は アイテムを つかった。"
    en = from_placeholders("<pc> uses an item.", "Taikan", "", pn="<pc>", sn="<kyodai>")
    assert en == "Taikan uses an item."


def test_to_from_placeholders_pc_convention_sibling():
    ja = "タイカンと キョウダイは いっしょに いる。"
    key = to_placeholders(ja, "タイカン", "キョウダイ", pn="<pc>", sn="<kyodai>")
    assert key == "<pc>と <kyodai>は いっしょに いる。"
    en = from_placeholders(
        "<pc> and <kyodai> are together.", "Taikan", "Sib", pn="<pc>", sn="<kyodai>"
    )
    assert en == "Taikan and Sib are together."


def test_to_from_placeholders_defaults_unchanged():
    """Defaults still produce the dialogue tokens (existing callers unaffected)."""
    assert to_placeholders("タイカンたちは　成功した！", "タイカン", "") == "<pnplacehold>たちは　成功した！"
    assert from_placeholders("<pnplacehold> and co.!", "Taikan", "") == "Taikan and co.!"
    assert to_placeholders("タイカン", "タイカン", "") == "<pnplacehold>"


# -------------------------------------------------------- _make_community_lookup dual-convention


def test_community_lookup_pc_convention_hits(tmp_path):
    """A <pc>-form quest/event entry hits when memory shows the literal player name."""
    c = TranslationCache(tmp_path / "pc.db")
    c.store("<pc>は アイテムを つかった。", "<pc> uses an item.", "community")
    t = Translator(c)
    lookup = _make_community_lookup(
        _cfg(player_name_ja="タイカン", player_name_en="Taikan"), t
    )
    assert lookup("タイカンは アイテムを つかった。") == "Taikan uses an item."
    c.close()


def test_community_lookup_pnplacehold_convention_still_hits(tmp_path):
    """Regression: the dialogue <pnplacehold> convention still hits."""
    c = TranslationCache(tmp_path / "pn.db")
    c.store("<pnplacehold>たちは　成功した！", "<pnplacehold> and co. succeeded!", "community")
    t = Translator(c)
    lookup = _make_community_lookup(
        _cfg(player_name_ja="タイカン", player_name_en="Taikan"), t
    )
    assert lookup("タイカンたちは　成功した！") == "Taikan and co. succeeded!"
    c.close()


def test_community_lookup_no_name_plain_entry_hits(tmp_path):
    """A no-name string looks up its plain entry under both conventions (key == ja)."""
    c = TranslationCache(tmp_path / "plain.db")
    c.store("コルット地方で新種発見？", "New species found in Coll region?", "community")
    t = Translator(c)
    lookup = _make_community_lookup(
        _cfg(player_name_ja="タイカン", player_name_en="Taikan"), t
    )
    assert lookup("コルット地方で新種発見？") == "New species found in Coll region?"
    c.close()


def test_community_lookup_miss_returns_none(tmp_path):
    c = TranslationCache(tmp_path / "miss.db")
    t = Translator(c)
    lookup = _make_community_lookup(
        _cfg(player_name_ja="タイカン", player_name_en="Taikan"), t
    )
    assert lookup("だれも しらない じゅもん。") is None
    c.close()
