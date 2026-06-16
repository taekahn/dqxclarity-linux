"""Tests for the proper-noun glossary (translate/glossary.py + its MT-path wiring).

These cover the behavioral contract ported from upstream (dqxclarity/app/common/translate.py
__glossify, db_ops.py generate_glossary_dict longest-first sort, update.py CSV format):

* longest-first wins — a term that is a *prefix* of a longer term must not clobber the longer one;
* glossify is a safe no-op when the glossary is empty/None (the offline guard);
* a known JA term is substituted in MT output, but a community/cache HIT is NOT re-glossified;
* regex-meta characters in JA keys are matched literally (re.escape), not as metacharacters;
* the CSV parser splits ``ja,en`` on the FIRST comma (English may contain commas).

Nothing here touches the network — every glossary is built from in-memory rows.
"""

from __future__ import annotations

from dqxclarity.translate.db import TranslationCache
from dqxclarity.translate.glossary import (
    Glossary,
    extract_glossary_from_zip,
    glossify,
    parse_glossary_csv,
    save_glossary,
)
from dqxclarity.translate.pipeline import Translator


def test_longest_first_wins_over_prefix():
    # "アスト" is a prefix of "アストルティア"; the longer term must win so the shorter one
    # doesn't clobber the larger name (db_ops.py:95-100 sorts JA keys longest-first).
    g = Glossary([("アスト", "Ast"), ("アストルティア", "Astoltia")])
    assert g.glossify("アストルティアの世界") == "Astoltia の世界"


def test_glossify_substitutes_padded_and_collapses_spaces():
    # Each hit is replaced with " en " (padded), back-to-back hits collapse the doubled space, and
    # the LEADING space is stripped (translate.py:45-50 uses lstrip only — the trailing pad space is
    # left for the MT engine to absorb, so it is preserved here too to stay faithful to upstream).
    g = Glossary([("ローレ", "Laure"), ("シア", "Sia")])
    assert g.glossify("ローレシア") == "Laure Sia "


def test_glossify_noop_when_empty():
    # An empty glossary is a no-op (the offline guard): text passes through unchanged.
    g = Glossary([])
    assert not g
    assert g.glossify("ぜんぜん変わらない") == "ぜんぜん変わらない"


def test_module_glossify_noop_when_none():
    # The module-level helper returns text unchanged when no glossary is available.
    assert glossify("変わらない", None) == "変わらない"


def test_glossify_handles_regex_meta_chars_in_keys():
    # A JA key containing regex metacharacters must match literally, not be interpreted as a
    # pattern (re.escape). Without escaping, "(" / "*" / "?" would raise or mis-match.
    g = Glossary([("(特技)", "Skill"), ("ホイミ*", "Heal")])
    assert g.glossify("(特技)を使う") == "Skill を使う"
    assert g.glossify("ホイミ*だ") == "Heal だ"


def test_parse_glossary_csv_splits_on_first_comma():
    # Rows are ``ja,en`` split on the FIRST comma so an English value may itself contain commas
    # (update.py:216-217). Blank lines and comma-less rows are skipped.
    csv = "アストルティア,Astoltia\n名前,Name, with comma\n\nbroken_line\n"
    rows = parse_glossary_csv(csv)
    assert ("アストルティア", "Astoltia") in rows
    assert ("名前", "Name, with comma") in rows  # english keeps its comma
    assert len(rows) == 2  # blank + comma-less lines dropped


def test_parse_glossary_csv_drops_quote_only_corrupt_rows():
    # The upstream glossary has a few poisoned rows whose EN is nothing but quote characters —
    # e.g. ``と頼まれた。`` ("...I was asked.") -> ``""""""``. That JA key is a ubiquitous sentence
    # ending, so substituting it injected six literal quotes into the MT source and shredded the
    # output (the dialogue-corruption bug). parse_glossary_csv must drop such rows on load.
    csv = 'と頼まれた。,""""""\nと言われた。,""""""\nスライム,Slime\n'
    rows = parse_glossary_csv(csv)
    assert ("スライム", "Slime") in rows
    assert all(en.strip('"') for _, en in rows)  # no row survives with a quote-only value
    assert len(rows) == 1  # both quote-only rows dropped


def test_parse_glossary_csv_keeps_legit_punctuation_and_ja_normalizations():
    # The quote-only filter is surgical: it must NOT drop legitimate rows whose value is plain
    # punctuation (``？？？``->``???`` is a real DQX placeholder name) or a JA->JA speech
    # normalization (``ワガハイ``->``わがはい`` feeds cleaner Japanese to MT).
    csv = "？？？,???\n（）,()\nワガハイ,わがはい\nでアール,である\n"
    rows = parse_glossary_csv(csv)
    assert ("？？？", "???") in rows
    assert ("（）", "()") in rows
    assert ("ワガハイ", "わがはい") in rows
    assert ("でアール", "である") in rows
    assert len(rows) == 4


def test_glossify_no_longer_injects_quotes_on_common_ending():
    # End-to-end regression: the poisoned row, once filtered, leaves the common sentence ending
    # untouched instead of replacing it with quotes.
    from dqxclarity.translate.glossary import Glossary

    poisoned = Glossary(parse_glossary_csv('と頼まれた。,""""""\nスライム,Slime\n'))
    assert poisoned.glossify("スライムと頼まれた。") == "Slime と頼まれた。"
    assert '"' not in poisoned.glossify("と頼まれた。")


def test_save_load_roundtrip(tmp_path):
    # save_glossary writes ``ja,en`` rows that parse_glossary_csv reads back unchanged for normal
    # rows (no embedded commas — the real upstream glossary keys/values).
    rows = [
        ("アストルティア", "Astoltia"),
        ("スライム", "Slime"),
        ("ドラキー", "Dracky"),
    ]
    path = save_glossary(rows, tmp_path)
    loaded = parse_glossary_csv(path.read_bytes())
    assert loaded == rows
    # Comma limitation (documented, not exercised as a round-trip): a comma in a JA KEY would NOT
    # round-trip, because parse_glossary_csv splits on the first comma and would mis-read the key's
    # tail as part of the English. A comma in an EN VALUE also breaks the round-trip, because
    # save_glossary's csv.writer RFC-4180-quotes the field ("a, b") but parse_glossary_csv does a
    # plain first-comma split that does not strip those quotes. The upstream glossary keys never
    # contain commas, so this is acceptable.


def test_extract_glossary_from_zip_reads_csv_member():
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("repo-main/csv/glossary.csv", "スライム,Slime\nドラキー,Dracky\n")
        zf.writestr("repo-main/csv/merge.xlsx", b"not a glossary")
    rows = extract_glossary_from_zip(buf.getvalue())
    assert ("スライム", "Slime") in rows
    assert ("ドラキー", "Dracky") in rows


def test_extract_glossary_from_zip_missing_is_empty():
    # No glossary member -> empty list (degrades to a no-op glossary, never raises).
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("repo-main/json/foo.json", "{}")
    assert extract_glossary_from_zip(buf.getvalue()) == []


class _CapturingProvider:
    """A fake MT provider that records exactly what text it was asked to translate."""

    name = "googletranslatefree"  # rank 1

    def __init__(self):
        self.seen: list[str] = []

    def available(self):
        return True

    def translate(self, texts):
        self.seen.extend(texts)
        return [t + " [MT]" for t in texts]


def test_mt_input_is_glossified(tmp_path):
    # A known JA term IS substituted in the text handed to the machine translator: the provider
    # sees the glossified input (matching upstream __api_translate, translate.py:220-221).
    c = TranslationCache(tmp_path / "g.db")
    prov = _CapturingProvider()
    g = Glossary([("アストルティア", "Astoltia")])
    t = Translator(c, sync_provider=prov, glossary=g)
    t.translate_now("アストルティアの冒険")
    assert prov.seen == ["Astoltia の冒険"]  # provider got the glossified JA
    c.close()


def test_community_cache_hit_is_not_glossified(tmp_path):
    # A community/cache HIT is served verbatim and is NEVER passed through the glossary or the MT
    # provider — only genuine machine translations are glossified.
    c = TranslationCache(tmp_path / "h.db")
    c.store("アストルティアの冒険", "The Adventure in Astoltia", "community")
    prov = _CapturingProvider()
    g = Glossary([("アストルティア", "WRONG")])  # would corrupt the string if applied
    t = Translator(c, sync_provider=prov, glossary=g)
    assert t.translate_now("アストルティアの冒険") == "The Adventure in Astoltia"  # untouched
    assert prov.seen == []  # provider never called -> nothing glossified on a cache hit
    c.close()
