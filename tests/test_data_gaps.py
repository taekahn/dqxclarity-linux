"""Regression tests for two TRANSLATION-DATA "imported a subset, not the whole source" gaps.

GAP #18 — custom JSON files are imported WHOLESALE by globbing every ``*/json/*.json`` member of
the dqx-custom-translations repo archive zip (translate/community.import_custom_zip /
sync_custom_supplements), instead of a hand-picked filename list. This must pull in files the old
list never named (e.g. custom_corner_text, custom_npc_name_overrides).

GAP #15 — the "Story So Far" sheet in merge.xlsx falls back to the DeepL machine-translation column
when the fixed-english cell is empty (translate/community.parse_merge_xlsx). Covered recaps still
prefer fixed-english; other sheets keep their fixed-english-only behavior.

All fixtures are tiny in-memory zip/xlsx bytes; nothing here touches the network.
"""

from __future__ import annotations

import io
import json
import zipfile

import openpyxl

from dqxclarity.translate.community import (
    import_custom_zip,
    parse_merge_xlsx,
    sync_custom_supplements,
)
from dqxclarity.translate.db import TranslationCache


def _b(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def _make_custom_zip(files: dict[str, bytes]) -> bytes:
    """Build a repo-archive-style zip: members are paths like ``<repo>-main/json/<file>.json``."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _make_merge_xlsx(sheets: dict[str, list[list]]) -> bytes:
    """Build an in-memory merge.xlsx. ``sheets`` maps sheet name -> list of rows (incl. header)."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)  # drop the default empty sheet
    for name, rows in sheets.items():
        ws = wb.create_sheet(title=name)
        for row in rows:
            ws.append(row)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


# --------------------------------------------------------------------------------------------------
# GAP #18 — wholesale glob of custom json files
# --------------------------------------------------------------------------------------------------

# The old hand-picked list (now removed). The glob MUST pull in strictly more than this set.
_OLD_HANDPICKED = {
    "custom_episode_request_book.json",
    "custom_master_quests.json",
    "custom_quest_rewards.json",
    "custom_seminar_questions_answers.json",
    "custom_team_quests.json",
}


def _custom_zip_fixture() -> bytes:
    # Representative of the real repo: the old hand-picked files PLUS files the old list never named
    # (custom_corner_text, custom_npc_name_overrides), all nested under .../json/, plus noise the
    # glob must ignore (a csv/ member and a json file outside json/).
    files = {
        "dqx-custom-translations-main/json/custom_quest_rewards.json": _b({"褒美": "Reward"}),
        "dqx-custom-translations-main/json/custom_team_quests.json": _b({"団クエ": "Team Quest"}),
        "dqx-custom-translations-main/json/custom_master_quests.json": _b({"匠": "Master Quest"}),
        "dqx-custom-translations-main/json/custom_episode_request_book.json": _b({"依頼": "Request"}),
        "dqx-custom-translations-main/json/custom_seminar_questions_answers.json": _b({"問": "Q&A"}),
        # NOT in the old hand-picked list — these are what the running hooks need:
        "dqx-custom-translations-main/json/custom_corner_text.json": _b({"看板": "Sign"}),
        "dqx-custom-translations-main/json/custom_npc_name_overrides.json": _b({"村人A": "Villager A"}),
        # Noise the glob must ignore:
        "dqx-custom-translations-main/csv/merge.xlsx": b"not json",
        "dqx-custom-translations-main/README.json": _b({"x": "y"}),  # json but NOT under json/
        "dqx-custom-translations-main/json/": b"",  # directory entry
    }
    return _make_custom_zip(files)


def test_glob_imports_more_than_old_handpicked_set(tmp_path):
    cache = TranslationCache(tmp_path / "c.db")
    files, rows = import_custom_zip(_custom_zip_fixture(), cache)

    # 7 json members under json/ (5 old + 2 new), 1 row each.
    assert files == 7
    assert rows == 7
    # Strictly MORE than the 5 old hand-picked files were imported.
    assert files > len(_OLD_HANDPICKED)

    # The previously-hand-picked files still import.
    assert cache.lookup("褒美") == "Reward"
    assert cache.lookup("団クエ") == "Team Quest"
    # The files the old list NEVER named are now imported — the whole point of the wholesale glob.
    assert cache.lookup("看板") == "Sign"
    assert cache.lookup("村人A") == "Villager A"
    cache.close()


def test_glob_ignores_non_json_dir_members_and_json_outside_json_dir(tmp_path):
    cache = TranslationCache(tmp_path / "c.db")
    import_custom_zip(_custom_zip_fixture(), cache)
    # The csv/merge.xlsx member and the directory entry contributed nothing.
    # README.json lives outside json/ so it must NOT be imported.
    assert cache.lookup("x") is None
    cache.close()


def test_sync_custom_supplements_globs_whole_zip(tmp_path, monkeypatch):
    # sync_custom_supplements fetches the repo archive ONCE and globs every json member, instead of
    # GETting a hand-picked filename list one-by-one.
    from dqxclarity.translate import community

    captured: list[str] = []

    class _Resp:
        def __init__(self, content: bytes):
            self.content = content

        def raise_for_status(self):
            pass

    def fake_get(url, **kwargs):
        captured.append(url)
        return _Resp(_custom_zip_fixture())

    monkeypatch.setattr(community.httpx, "get", fake_get)
    cache = TranslationCache(tmp_path / "c.db")
    imported = sync_custom_supplements(cache)

    # Exactly one network fetch — the whole-repo archive — not one per file.
    assert len(captured) == 1
    assert captured[0] == community.CUSTOM_TRANSLATIONS_ZIP_URL
    assert imported == 7
    assert cache.lookup("看板") == "Sign"  # a file the old hand-picked list never named
    cache.close()


# --------------------------------------------------------------------------------------------------
# GAP #15 — Story So Far DeepL fallback
# --------------------------------------------------------------------------------------------------

# merge.xlsx "Story So Far" layout (mirrors upstream update.py:194-196): col1 source/JA,
# col2 DeepL machine translation, col3 fixed english.
_SSF_HEADER = ["Source (Japanese)", "DeepL Translation", "Fixed English"]


def test_story_so_far_falls_back_to_deepl_when_fixed_en_empty():
    data = _make_merge_xlsx(
        {
            "Story So Far": [
                _SSF_HEADER,
                ["物語1", "DeepL Recap One", "Fixed Recap One"],  # both present
                ["物語2", "DeepL Recap Two", None],  # fixed-en empty -> fall back to DeepL
                ["物語3", "DeepL Recap Three", ""],  # fixed-en empty string -> fall back to DeepL
            ],
        }
    )
    rows = dict(parse_merge_xlsx(data))
    # Prefers fixed-english when both exist.
    assert rows["物語1"] == "Fixed Recap One"
    # Recovers rows that previously would have been dropped (only DeepL available).
    assert rows["物語2"] == "DeepL Recap Two"
    assert rows["物語3"] == "DeepL Recap Three"
    assert len(rows) == 3


def test_story_so_far_row_with_no_translation_at_all_is_dropped():
    data = _make_merge_xlsx(
        {
            "Story So Far": [
                _SSF_HEADER,
                ["物語A", None, None],  # neither DeepL nor fixed-en -> dropped
                ["物語B", "DeepL B", None],  # DeepL only -> kept
            ],
        }
    )
    rows = dict(parse_merge_xlsx(data))
    assert "物語A" not in rows
    assert rows["物語B"] == "DeepL B"
    assert len(rows) == 1


def test_non_story_sheets_do_not_fall_back_to_deepl():
    # The Dialogue sheet has the same 3-column shape, but it must NOT fall back to col2 — only
    # "Story So Far" gets the DeepL fallback. A Dialogue row with empty fixed-english is dropped.
    data = _make_merge_xlsx(
        {
            "Dialogue": [
                ["Source (Japanese)", "DeepL Translation", "Fixed English"],
                ["会話1", "DeepL Dialogue", None],  # fixed-en empty -> DROPPED (no fallback)
                ["会話2", "DeepL Dialogue 2", "Fixed Dialogue"],  # fixed-en present -> kept
            ],
        }
    )
    rows = dict(parse_merge_xlsx(data))
    assert "会話1" not in rows  # no DeepL fallback for Dialogue
    assert rows["会話2"] == "Fixed Dialogue"
    assert len(rows) == 1


def test_story_and_other_sheets_combined_behavior():
    # End-to-end: a workbook with Dialogue + Story So Far. Only Story So Far recovers DeepL-only rows.
    data = _make_merge_xlsx(
        {
            "Dialogue": [
                ["Source", "DeepL", "Fixed English"],
                ["d_ja", "d_deepl", None],  # dropped (no fallback)
                ["d_ja2", "d_deepl2", "d_fixed"],  # kept via fixed-en
            ],
            "Story So Far": [
                _SSF_HEADER,
                ["s_ja", "s_deepl", None],  # kept via DeepL fallback
                ["s_ja2", "s_deepl2", "s_fixed"],  # kept via fixed-en (preferred)
            ],
        }
    )
    rows = dict(parse_merge_xlsx(data))
    assert "d_ja" not in rows
    assert rows["d_ja2"] == "d_fixed"
    assert rows["s_ja"] == "s_deepl"
    assert rows["s_ja2"] == "s_fixed"
    assert len(rows) == 3
