"""Inline control-tag preservation through machine-translated dialogue (task #3).

The reconstruction in ``_translate_body`` used to keep only the text runs between the first and
last visible segment, silently dropping any tag that sat *between* two text runs — so an inline
``<color_yellow>`` warning lost its colour, and a ``textA<color_yellow>textB<color_white>textC``
shape lost both colour tags (leaving the box's colour state unbalanced). The fix interleaves every
tag in original order and wraps with ``wrap_tagged`` (tags are zero-width and unbreakable).

A fake translator (per-segment echo, no network) is used throughout: each Japanese segment is
pre-stored in the cache under its ``normalize_source`` key, so ``_xlate``'s ``lookup`` hits.
"""

from __future__ import annotations

import re

from dqxclarity.translate.db import TranslationCache
from dqxclarity.translate.dialogue import translate_conversation
from dqxclarity.translate.pipeline import Translator
from dqxclarity.translate.wrap import normalize_source, wrap_tagged

_TAG = re.compile(r"<[^>]*>")


def _visible(line: str) -> str:
    """A line with all <...> tags removed (the on-screen characters that count toward width)."""
    return _TAG.sub("", line)


def _cache_with(tmp_path, name, pairs):
    """Build a Translator whose cache maps normalize_source(ja_segment) -> en for each pair."""
    c = TranslationCache(tmp_path / name)
    for ja, en in pairs:
        c.store(normalize_source(ja), en, "community")
    return Translator(c)


# --------------------------------------------------------------------------------- wrap_tagged unit


def test_wrap_tagged_keeps_inline_color_tag():
    s = "Proceed to the Rest Area?<color_yellow>Your vocation will change and you lose items."
    out = wrap_tagged(s, width=46, lines_per_page=0)
    assert "<color_yellow>" in out


def test_wrap_tagged_no_visible_line_exceeds_width():
    s = (
        "Proceed to the Rest Area?<color_yellow>Your vocation will change to Magic Warrior and "
        "you will lose access to all of your held items until you leave."
    )
    out = wrap_tagged(s, width=46, lines_per_page=0)
    for line in out.split("\n"):
        # tags are zero-width; only the visible characters count toward the box width
        assert len(_visible(line)) <= 46, repr(line)


def test_wrap_tagged_never_splits_a_tag_across_lines():
    s = (
        "Proceed to the Rest Area?<color_yellow>Your vocation will change to Magic Warrior and "
        "you will lose access to all of your held items.<color_white>Items return outside."
    )
    out = wrap_tagged(s, width=46, lines_per_page=0)
    # every tag survives whole on exactly one line (no '<' or '>' orphaned by a line break)
    for tag in ("<color_yellow>", "<color_white>"):
        assert any(tag in line for line in out.split("\n"))
    # a line never starts or ends mid-tag
    for line in out.split("\n"):
        assert line.count("<") == line.count(">")


def test_wrap_tagged_breaks_oversized_word():
    """A single word longer than the box width is hard-broken so no line overflows (I1)."""
    long_word = "Supercalifragilisticexpialidociousandthensomemoreletters" * 2  # > 46 chars
    assert len(long_word) > 46
    out = wrap_tagged(f"See {long_word} now", width=46, lines_per_page=0)
    for line in out.split("\n"):
        assert len(_visible(line)) <= 46, repr(line)
    # the word survives whole — only line breaks were inserted, no characters lost
    assert long_word in out.replace("\n", "")


def test_wrap_tagged_oversized_word_keeps_leading_tag():
    """An oversized word carrying a leading colour tag keeps that tag on its first chunk."""
    long_word = "x" * 120
    out = wrap_tagged(f"<color_yellow>{long_word}<color_white>", width=46, lines_per_page=0)
    for line in out.split("\n"):
        assert len(_visible(line)) <= 46, repr(line)
    assert out.split("\n")[0].startswith("<color_yellow>")
    # both tags survive, none split
    assert "<color_yellow>" in out and "<color_white>" in out
    for line in out.split("\n"):
        assert line.count("<") == line.count(">")


def test_wrap_tagged_empty_and_tag_only():
    """Empty / whitespace-only input yields empty; an all-tags string passes through intact."""
    assert wrap_tagged("", width=46, lines_per_page=0) == ""
    assert wrap_tagged("   ", width=46, lines_per_page=3) == ""
    assert wrap_tagged("<close>", width=46, lines_per_page=0) == "<close>"
    assert wrap_tagged("<yesno><close>   ", width=46, lines_per_page=0) == "<yesno><close>"


def test_wrap_tagged_color_open_and_close_both_survive():
    s = "Hello there<color_yellow>this is the warning text part<color_white>and back to normal."
    out = wrap_tagged(s, width=46, lines_per_page=0)
    assert "<color_yellow>" in out and "<color_white>" in out
    assert out.index("<color_yellow>") < out.index("<color_white>")


def test_wrap_tagged_lpp_zero_no_br():
    s = (
        "<color_yellow>A deliberately very long string that would normally wrap across several "
        "lines and would get page break markers inserted by the paginator if it were enabled."
    )
    out = wrap_tagged(s, width=46, lines_per_page=0)
    assert "<br>" not in out


def test_wrap_tagged_lpp_three_paginates():
    s = (
        "<color_yellow>A deliberately very long string that would normally wrap across several "
        "lines and so the paginator inserts at least one page break marker into the output here."
    )
    out = wrap_tagged(s, width=46, lines_per_page=3)
    assert "<br>" in out


# ------------------------------------------------------- _translate_body / translate_conversation


# The real game string (JA) and a per-segment English echo from the community DB.
_REST_JA = (
    "休息の間に進んで　よろしいですか？\n"
    "<color_yellow>※職業が魔闘士になり　持ち物が無くなります。\n"
    "　職業と持ち物は　外へ出ると　元に戻ります。<yesno><close>"
)
_REST_SEG1_JA = "休息の間に進んで　よろしいですか？"
_REST_SEG2_JA = (
    "※職業が魔闘士になり　持ち物が無くなります。\n　職業と持ち物は　外へ出ると　元に戻ります。"
)
_REST_SEG1_EN = "Proceed to the Rest Area?"
_REST_SEG2_EN = (
    "Your vocation will change to Magic Warrior and you will lose access to your held items. "
    "Your vocation and items return to normal when you leave."
)


def test_rest_area_inline_color_preserved(tmp_path):
    t = _cache_with(
        tmp_path,
        "rest.db",
        [(_REST_SEG1_JA, _REST_SEG1_EN), (_REST_SEG2_JA, _REST_SEG2_EN)],
    )
    out = translate_conversation(t, _REST_JA, width=46, lines_per_page=3)
    assert out is not None
    # the warning stays yellow (inline color tag not dropped)
    assert "<color_yellow>" in out
    # exactly one <color tag in -> exactly one out (no drop, no duplication)
    assert out.count("<color") == _REST_JA.count("<color")
    # the terminators ride the very end of the output
    assert out.rstrip().endswith("<yesno><close>")
    # both English runs made it through
    assert "Proceed to the Rest Area?" in _visible(out)
    assert "Magic Warrior" in _visible(out)


def test_rest_area_visible_width_respected(tmp_path):
    t = _cache_with(
        tmp_path,
        "rest2.db",
        [(_REST_SEG1_JA, _REST_SEG1_EN), (_REST_SEG2_JA, _REST_SEG2_EN)],
    )
    out = translate_conversation(t, _REST_JA, width=46, lines_per_page=3)
    assert out is not None
    for line in out.split("\n"):
        if line == "<br>":
            continue
        assert len(_visible(line)) <= 46, repr(line)


def test_textA_color_textB_color_textC_both_tags_survive_in_order(tmp_path):
    ja = "あいう<color_yellow>かきく<color_white>さしす"
    t = _cache_with(
        tmp_path,
        "abc.db",
        [("あいう", "Alpha"), ("かきく", "Bravo"), ("さしす", "Charlie")],
    )
    out = translate_conversation(t, ja, width=46, lines_per_page=3)
    assert out is not None
    assert "<color_yellow>" in out and "<color_white>" in out
    assert out.index("<color_yellow>") < out.index("<color_white>")
    # no tag dropped
    assert out.count("<color") == 2
    # text runs preserved in order
    vis = _visible(out)
    assert vis.index("Alpha") < vis.index("Bravo") < vis.index("Charlie")


def test_leading_only_color_single_run_regression(tmp_path):
    # leading + trailing color around a single text run: must stay correct (passed before the fix).
    ja = "<color_yellow>先に…捧げられません！<color_white>"
    seg = "先に…捧げられません！"
    t = _cache_with(tmp_path, "lead.db", [(seg, "You cannot offer that first!")])
    out = translate_conversation(t, ja, width=46, lines_per_page=3)
    assert out is not None
    assert "<color_yellow>" in out and "<color_white>" in out
    assert out.index("<color_yellow>") < out.index("<color_white>")
    assert "You cannot offer that first!" in _visible(out)


def test_quest_profile_lpp_zero_no_br(tmp_path):
    t = _cache_with(
        tmp_path,
        "quest.db",
        [(_REST_SEG1_JA, _REST_SEG1_EN), (_REST_SEG2_JA, _REST_SEG2_EN)],
    )
    out = translate_conversation(t, _REST_JA, width=46, lines_per_page=0)
    assert out is not None
    assert "<br>" not in out
    assert "<color_yellow>" in out  # color still preserved on the no-pagination surface


def test_dialogue_profile_lpp_three_paginates(tmp_path):
    long_en = (
        "Your vocation will change to Magic Warrior and you will lose access to all of your held "
        "items until you decide to leave this rest area and return to the outside world again."
    )
    t = _cache_with(
        tmp_path,
        "dlg.db",
        [(_REST_SEG1_JA, _REST_SEG1_EN), (_REST_SEG2_JA, long_en)],
    )
    out = translate_conversation(t, _REST_JA, width=46, lines_per_page=3)
    assert out is not None
    assert "<br>" in out  # the dialogue box still paginates


def test_tag_only_string_passes_through_unchanged(tmp_path):
    # No Japanese text -> the control tags pass through untouched.
    c = TranslationCache(tmp_path / "tagonly.db")
    t = Translator(c)
    ja = "<close>"
    out = translate_conversation(t, ja, width=46, lines_per_page=3)
    assert out == "<close>"

    ja2 = "<yesno><close>"
    out2 = translate_conversation(t, ja2, width=46, lines_per_page=3)
    assert out2 == "<yesno><close>"
