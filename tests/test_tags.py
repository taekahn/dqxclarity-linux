"""Unit tests for the MT tag-protection layer (#14).

Every known variable/color tag must survive a round-trip through ``protect_tags`` -> a
*mangled* machine-translation pass -> ``restore_tags`` and come back as its original tag. We
simulate the ways MT actually mangles sentinels: lowercasing, inserting/stripping stray spaces,
dropping the leading ``<``, and wrapping the line in translated words.
"""

from __future__ import annotations

import re

import pytest

from dqxclarity.translate.tags import (
    _KYODAI_REL_TAGS,
    _TAG_SENTINELS,
    protect_tags,
    restore_tags,
    sibling_word,
)

# Every fixed (non-sibling) tag the table protects.
ALL_FIXED_TAGS = [tag for tag, _sentinel in _TAG_SENTINELS]


def _mangle_spaces(text: str) -> str:
    """Simulate MT sprinkling stray spaces around the sentinel's punctuation/underscores."""
    text = text.replace("<&", "< &")
    text = text.replace("_", " _ ")
    text = text.replace(">", " >")
    return text


# --------------------------------------------------------------------------- #
# Fixed placeholder tags                                                       #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("tag", ALL_FIXED_TAGS)
def test_fixed_tag_clean_roundtrip(tag: str) -> None:
    """A clean (unmangled) protect -> restore returns the original tag exactly."""
    sentence = f"Hello {tag} world"
    protected = protect_tags(sentence)
    assert tag not in protected, f"{tag} should have been swapped to a sentinel"
    restored = restore_tags(protected)
    assert restored == sentence


@pytest.mark.parametrize("tag", ALL_FIXED_TAGS)
def test_fixed_tag_lowercased_roundtrip(tag: str) -> None:
    """MT lowercasing the sentinel must not defeat restore."""
    protected = protect_tags(f"x {tag} y")
    mangled = protected.lower()
    assert restore_tags(mangled) == f"x {tag} y"


@pytest.mark.parametrize("tag", ALL_FIXED_TAGS)
def test_fixed_tag_stray_spaces_roundtrip(tag: str) -> None:
    """MT inserting stray spaces inside the sentinel must not defeat restore."""
    protected = protect_tags(f"start {tag} end")
    mangled = _mangle_spaces(protected)
    assert tag in restore_tags(mangled)


@pytest.mark.parametrize("tag", ALL_FIXED_TAGS)
def test_fixed_tag_dropped_leading_bracket_roundtrip(tag: str) -> None:
    """MT occasionally eats the leading ``<`` of a sentinel; restore tolerates it."""
    protected = protect_tags(f"a {tag} b")
    # Drop the FIRST "<" of the sentinel (the table sentinels each contain exactly one "<").
    mangled = protected.replace("<&", "&", 1)
    assert tag in restore_tags(mangled)


@pytest.mark.parametrize("tag", ALL_FIXED_TAGS)
def test_fixed_tag_dropped_a_run_roundtrip(tag: str) -> None:
    """MT dropping one ``a`` from the sentinel's repeated padding run must not defeat restore.

    The padding runs (e.g. ``<&13_aaaaaaa>``) are the part MT most often compresses or expands by
    a character; an exact-count restore would silently leak the raw sentinel. Mirrors upstream's
    ±1 length-variant swap_back (translate.py:105-159), generalized to any run length.
    """
    protected = protect_tags(f"head {tag} tail")
    # Strip one "a" from the FIRST run of 3+ a's in the protected sentinel.
    mangled, n = re.subn(r"a(aa+)", r"\1", protected, count=1)
    if n == 0:
        pytest.skip(f"sentinel for {tag} has no run of 3+ a's to mangle")
    assert mangled != protected, "the run-shortening mangle must have changed the sentinel"
    assert tag in restore_tags(mangled)


def test_all_fixed_tags_in_one_string() -> None:
    """All fixed tags together, comma-joined, round-trip with the order preserved."""
    sentence = ", ".join(ALL_FIXED_TAGS)
    restored = restore_tags(protect_tags(sentence))
    assert restored == sentence


# --------------------------------------------------------------------------- #
# Color tags                                                                   #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("color", ["red", "blue", "green", "yellow", "white123"])
def test_color_tag_roundtrip(color: str) -> None:
    tag = f"<color_{color}>"
    protected = protect_tags(f"see {tag} this")
    assert tag not in protected
    assert "<&color_" in protected
    assert restore_tags(protected) == f"see {tag} this"


def test_color_tag_lowercased_and_spaced() -> None:
    tag = "<color_red>"
    protected = protect_tags(f"a {tag} b").lower().replace("<&", "< &")
    assert restore_tags(protected) == "a <color_red> b"


def test_color_tag_dropped_leading_bracket() -> None:
    tag = "<color_red>"
    protected = protect_tags(f"a {tag} b").replace("<&color", "&color")
    assert restore_tags(protected) == "a <color_red> b"


# --------------------------------------------------------------------------- #
# Sibling-relationship tags <kyodai_rel1/2/3>                                  #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "byte_value,expected",
    [
        (1, "brother"),  # older brother
        (2, "brother"),  # younger brother
        (3, "sister"),  # older sister
        (4, "sister"),  # younger sister
        (0, "sibling"),  # unknown
        (None, "sibling"),  # unset
        (99, "sibling"),  # out-of-range -> default
    ],
)
def test_sibling_word_mapping(byte_value, expected: str) -> None:
    assert sibling_word(byte_value) == expected


@pytest.mark.parametrize("tag", _KYODAI_REL_TAGS)
@pytest.mark.parametrize(
    "byte_value,expected",
    [(1, "brother"), (2, "brother"), (3, "sister"), (4, "sister"), (0, "sibling"), (None, "sibling")],
)
def test_kyodai_rel_resolves_to_english_word(tag: str, byte_value, expected: str) -> None:
    """<kyodai_rel*> becomes the correct English word, surviving a clean round-trip."""
    protected = protect_tags(f"My {tag} is here", sibling_relationship=byte_value)
    assert tag not in protected, "raw sibling tag must not survive protect"
    restored = restore_tags(protected)
    assert restored == f"My {expected} is here"


def test_kyodai_rel_word_survives_mangled_sentinel() -> None:
    """Even if MT lowercases / spaces the wrapping sentinel, the chosen word is recovered."""
    protected = protect_tags("the <kyodai_rel1> waits", sibling_relationship=3)
    mangled = protected.lower().replace("<&", "< &").replace("_", " _ ")
    assert restore_tags(mangled) == "the sister waits"


def test_kyodai_rel_no_raw_sentinel_leaks() -> None:
    """No <&7_aa> sentinel fragment may leak into the restored string."""
    protected = protect_tags("<kyodai_rel2>", sibling_relationship=1)
    restored = restore_tags(protected)
    assert restored == "brother"
    assert "&7" not in restored and "<&" not in restored


def test_kyodai_rel_stray_half_sentinel_collapses() -> None:
    """A lone half-sentinel (MT dropped one side) must not leak; it collapses to nothing."""
    # Simulate MT keeping the word + only the leading sentinel half.
    leaked = "result <&7_aa>brother"
    restored = restore_tags(leaked)
    assert "&7" not in restored
    assert "brother" in restored


# --------------------------------------------------------------------------- #
# No-tag text                                                                  #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "plain",
    [
        "Just an ordinary sentence.",
        "Numbers 123 and symbols !@#$ stay put.",
        "",
        "A line with < and > but no real tags.",
        "Email me at someone@example.com please.",
    ],
)
def test_plain_text_unchanged(plain: str) -> None:
    protected = protect_tags(plain)
    assert protected == plain
    assert restore_tags(protected) == plain


def test_restore_is_noop_on_untouched_text() -> None:
    """restore_tags on text that never went through protect leaves it alone."""
    text = "Nothing to restore here <color_> incomplete and <pc literal."
    assert restore_tags(text) == text


# --------------------------------------------------------------------------- #
# Prefix-collision safety                                                      #
# --------------------------------------------------------------------------- #
def test_prefix_tags_do_not_clobber_each_other() -> None:
    """<pc_hiryu>/<pc>/<pc_race> etc. must each survive when they appear together."""
    sentence = "<pc> and <pc_hiryu> and <pc_race> and <pc_hometown> and <pc_syokugyo>"
    assert restore_tags(protect_tags(sentence)) == sentence


def test_title_tags_distinct_from_suit_glyphs() -> None:
    """<&20_...> title sentinels must not be eaten by the <&2a> heart sentinel pattern."""
    sentence = "<1st_title> <heart> <2nd_title> <diamond>"
    assert restore_tags(protect_tags(sentence)) == sentence


def test_pc_rel_distinct_from_kyodai_rel() -> None:
    """<pc_rel1> (a fixed tag) must NOT be resolved as a sibling word."""
    sentence = "<pc_rel1> <pc_rel2> <pc_rel3>"
    protected = protect_tags(sentence, sibling_relationship=1)
    restored = restore_tags(protected)
    assert restored == sentence
    # And it must not have leaked a sibling word.
    assert "brother" not in restored


def test_sentinels_are_unique() -> None:
    """Sanity: no two fixed tags share a sentinel (else restore would be ambiguous)."""
    sentinels = [s for _t, s in _TAG_SENTINELS]
    assert len(sentinels) == len(set(sentinels))


def test_table_has_expected_tag_count() -> None:
    """40+ tags protected: 37 fixed + 3 sibling = 40 (mirrors upstream's table)."""
    assert len(ALL_FIXED_TAGS) == 37
    assert len(_KYODAI_REL_TAGS) == 3
    assert len(ALL_FIXED_TAGS) + len(_KYODAI_REL_TAGS) >= 40
