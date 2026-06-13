"""Tests for #23 BAD STRING suppression.

Covers:
  * SuppressionIndex SUBSTRING matching (upstream search_bad_strings: ``ja in text``) returns the
    curated EN fallback; a non-match falls through to None.
  * placeholder <-> live-name substitution: a placeholder-keyed suppression matches literal-name
    text and vice-versa, and the returned EN gets the live English name swapped in.
  * the dispatch translate_fn runs the suppression pre-pass BEFORE community_lookup/MT.
  * community.parse_merge_bad_strings splits bad_string=1 suppressions from bad_string=0 fixes, and
    a bad_string=0 fix is keyed/looked-up on the CORRECT (column-5 original) ja.
"""

from __future__ import annotations

import io
from types import SimpleNamespace

import openpyxl
import pytest

from dqxclarity.translate.community import (
    load_suppressions,
    load_suppressions_local,
    parse_merge_bad_strings,
    save_suppressions,
    sync_community,
)
from dqxclarity.translate.db import TranslationCache
from dqxclarity.translate.pipeline import Translator
from dqxclarity.translate.suppression import SuppressionIndex
from dqxclarity.runtime.dispatch import build_translate_fn


# --------------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------------


def _make_merge_xlsx(sheets: dict[str, list[list]]) -> bytes:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(title=name)
        for row in rows:
            ws.append(row)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _cfg(**over):
    tr = SimpleNamespace(
        player_name_ja="", player_name_en="", sibling_name_ja="", sibling_name_en="",
        wrap_width=46, lines_per_page=3,
    )
    for k, v in over.items():
        setattr(tr, k, v)
    return SimpleNamespace(translate=tr)


# --------------------------------------------------------------------------------------------------
# SuppressionIndex — substring match + non-match fall-through
# --------------------------------------------------------------------------------------------------


def test_substring_match_returns_fallback():
    idx = SuppressionIndex([("こわれた", "It broke.")])
    # The key is a SUBSTRING of the captured text (upstream ``ja in text``), so the fallback returns.
    assert idx.match("そのアイテムはこわれたようだ") == "It broke."


def test_exact_match_also_returns_fallback():
    idx = SuppressionIndex([("こわれた", "It broke.")])
    assert idx.match("こわれた") == "It broke."


def test_non_match_falls_through():
    idx = SuppressionIndex([("こわれた", "It broke.")])
    assert idx.match("べつのもじれつ") is None  # no substring -> None (falls through to cache/MT)


def test_first_match_wins_like_upstream_row_scan():
    idx = SuppressionIndex([("あ", "first"), ("あいうえお", "second")])
    # Both keys are substrings; the FIRST entry wins, mirroring upstream's row-by-row scan.
    assert idx.match("これはあいうえおです") == "first"


def test_empty_key_never_matches():
    # An empty key would be a substring of EVERYTHING and suppress all text — it must be dropped.
    idx = SuppressionIndex([("", "should never appear"), ("ねこ", "Cat")])
    assert len(idx) == 1
    assert idx.match("いぬ") is None
    assert idx.match("くろねこ") == "Cat"


def test_empty_index_matches_nothing():
    assert SuppressionIndex([]).match("なにか") is None


# --------------------------------------------------------------------------------------------------
# placeholder <-> live-name substitution (both directions) + EN name swap-in
# --------------------------------------------------------------------------------------------------


def test_placeholder_keyed_matches_literal_name_text():
    # Suppression keyed with the quest/event placeholder <pc>; captured text has the LITERAL JA name.
    idx = SuppressionIndex([("<pc>はやられた", "<pc> was defeated.")])
    out = idx.match(
        "たろうはやられた",
        player_ja="たろう", player_en="Tarou",
    )
    assert out == "Tarou was defeated."


def test_literal_name_keyed_matches_placeholder_text():
    # Suppression keyed with the LITERAL JA name; captured text carries the placeholder instead.
    idx = SuppressionIndex([("たろうはやられた", "Tarou-line broke.")])
    out = idx.match(
        "<pc>はやられた",
        player_ja="たろう", player_en="Tarou",
    )
    assert out == "Tarou-line broke."


def test_dialogue_placeholder_convention_matches_too():
    # The dialogue-corpus convention (<pnplacehold>) is tried as well as the quest one.
    idx = SuppressionIndex([("<pnplacehold>のへや", "<pnplacehold>'s room is broken.")])
    out = idx.match(
        "はなこのへやにはいる",
        player_ja="はなこ", player_en="Hanako",
    )
    assert out == "Hanako's room is broken."


def test_sibling_placeholder_substitution():
    idx = SuppressionIndex([("<kyodai>とはなす", "Talk to <kyodai> broke.")])
    out = idx.match(
        "けんとはなす",
        sibling_ja="けん", sibling_en="Ken",
    )
    assert out == "Talk to Ken broke."


def test_name_match_with_no_names_set_falls_through():
    # A placeholder-keyed entry with NO live names cannot align to literal-name text -> no match.
    idx = SuppressionIndex([("<pc>はやられた", "<pc> was defeated.")])
    assert idx.match("たろうはやられた") is None


# --------------------------------------------------------------------------------------------------
# dispatch: the suppression pre-pass runs BEFORE community_lookup / MT
# --------------------------------------------------------------------------------------------------


def test_dispatch_suppression_runs_before_cache_and_mt(tmp_path):
    cache = TranslationCache(tmp_path / "s.db")
    # Seed a cache entry that WOULD win if the suppression pass didn't run first.
    cache.store("こわれた文字列です", "cached translation", "community")
    translator = Translator(cache)
    idx = SuppressionIndex([("こわれた文字列", "SUPPRESSED")])
    fn, _ = build_translate_fn(_cfg(), translator, suppression=idx)
    # Substring suppression wins over the (longer-keyed) cache hit and any MT.
    assert fn("こわれた文字列です") == "SUPPRESSED"
    cache.close()


def test_dispatch_without_suppression_unchanged(tmp_path):
    cache = TranslationCache(tmp_path / "s2.db")
    cache.store("ねこがいる", "There is a cat.", "community")
    translator = Translator(cache)
    fn, _ = build_translate_fn(_cfg(), translator)  # no suppression -> previous behaviour
    assert fn("ねこがいる") == "There is a cat."
    cache.close()


def test_dispatch_suppression_miss_falls_through_to_cache(tmp_path):
    cache = TranslationCache(tmp_path / "s3.db")
    cache.store("いぬがいる", "There is a dog.", "community")
    translator = Translator(cache)
    idx = SuppressionIndex([("こわれた", "SUPPRESSED")])  # does not match
    fn, _ = build_translate_fn(_cfg(), translator, suppression=idx)
    assert fn("いぬがいる") == "There is a dog."  # falls through to the cache hit
    cache.close()


def test_dispatch_suppression_non_japanese_returns_none(tmp_path):
    cache = TranslationCache(tmp_path / "s4.db")
    translator = Translator(cache)
    idx = SuppressionIndex([("hello", "SUPPRESSED")])
    fn, _ = build_translate_fn(_cfg(), translator, suppression=idx)
    assert fn("Already English") is None  # non-Japanese short-circuits before suppression
    cache.close()


def test_dispatch_suppression_uses_live_translator_names(tmp_path):
    cache = TranslationCache(tmp_path / "s5.db")
    translator = Translator(cache)
    translator.player_name_ja = "たろう"
    translator.player_name_en = "Tarou"
    idx = SuppressionIndex([("<pc>はやられた", "<pc> was defeated.")])
    fn, _ = build_translate_fn(_cfg(), translator, suppression=idx)
    assert fn("たろうはやられた") == "Tarou was defeated."
    cache.close()


# --------------------------------------------------------------------------------------------------
# community.parse_merge_bad_strings — split suppressions vs. fixes; fixes keyed on the correct ja
# --------------------------------------------------------------------------------------------------


def test_parse_merge_bad_strings_splits_suppressions_and_fixes():
    data = _make_merge_xlsx(
        {
            "Dialogue": [
                ["Japanese", "DeepL", "Fixed English", "Notes", "Original Bad String Text"],
                # bad_string=1: BAD STRING note, NO original text -> SUPPRESSION on the partial ja.
                ["こわれた部分", "x", "It broke.", "BAD STRING", None],
                # bad_string=0: BAD STRING note WITH original text -> FIX keyed on the correct ja.
                ["だれかの部分文", "x", "Someone's line.", "BAD STRING", "本当のソース文字列"],
                # not a bad string -> ignored by this parser (handled by parse_merge_xlsx).
                ["ふつうの行", "x", "Normal line.", None, None],
            ],
        }
    )
    suppressions, fixes = parse_merge_bad_strings(data)
    assert suppressions == [("こわれた部分", "It broke.")]
    # The fix is keyed on the column-5 ORIGINAL (correct) ja, NOT the partial primary ja.
    assert fixes == [("本当のソース文字列", "Someone's line.")]


def test_parse_merge_bad_strings_marker_is_case_insensitive():
    data = _make_merge_xlsx(
        {
            "Dialogue": [
                ["Japanese", "DeepL", "Fixed English", "Notes", "Original Bad String Text"],
                ["へんなもじ", "x", "Bad.", "this is a bad string entry", None],
            ],
        }
    )
    suppressions, fixes = parse_merge_bad_strings(data)
    assert suppressions == [("へんなもじ", "Bad.")]
    assert fixes == []


def test_parse_merge_bad_strings_skips_rows_without_english():
    data = _make_merge_xlsx(
        {
            "Dialogue": [
                ["Japanese", "DeepL", "Fixed English", "Notes", "Original Bad String Text"],
                ["こわれた", "x", None, "BAD STRING", None],  # no en -> skipped
            ],
        }
    )
    suppressions, fixes = parse_merge_bad_strings(data)
    assert suppressions == [] and fixes == []


def test_parse_merge_bad_strings_only_scans_dialogue_sheet():
    # Upstream (update.py:134-158) applies BAD STRING / notes logic to the DIALOGUE sheet ONLY.
    # Walkthrough/Quests/Story So Far have no Notes or original-bad-string columns; their column-3
    # data must NOT be misread as a BAD STRING marker (the _notes_col fallback is fixed col 3). Here
    # a Walkthrough row carries "bad string" in a non-Notes position and a Quests row's column-4 holds
    # data that would be a spurious fix key — both MUST be ignored, only the Dialogue row counts.
    data = _make_merge_xlsx(
        {
            "Dialogue": [
                ["Japanese", "DeepL", "Fixed English", "Notes", "Original Bad String Text"],
                ["こわれた部分", "x", "It broke.", "BAD STRING", None],
            ],
            # No Notes/original header -> _notes_col falls back to fixed col 3. The literal
            # "bad string" sitting at col 3 here must NOT trigger a suppression on a Walkthrough row.
            "Walkthrough": [
                ["Japanese", "Fixed English", "Misc", "bad string"],
                ["ウォークスルー行", "Walk line.", "note", "bad string"],
            ],
            # Quests: col-4 data that would become a spurious fix key if scanned -> must be ignored.
            "Quests": [
                ["Japanese", "Fixed English", "Misc", "bad string", "本当ではないキー"],
                ["クエスト行", "Quest line.", "n", "bad string", "本当ではないキー"],
            ],
        }
    )
    suppressions, fixes = parse_merge_bad_strings(data)
    # Only the Dialogue BAD STRING row is collected; the other sheets are not scanned at all.
    assert suppressions == [("こわれた部分", "It broke.")]
    assert fixes == []
    # Specifically, no Walkthrough/Quests data leaked in.
    assert ("ウォークスルー行", "Walk line.") not in suppressions
    assert ("本当ではないキー", "Quest line.") not in fixes


def test_load_suppressions_fetches_and_returns_entries(monkeypatch):
    data = _make_merge_xlsx(
        {
            "Dialogue": [
                ["Japanese", "DeepL", "Fixed English", "Notes", "Original Bad String Text"],
                ["こわれた部分", "x", "It broke.", "BAD STRING", None],
            ],
        }
    )

    class _Resp:
        content = data

        def raise_for_status(self):
            pass

    monkeypatch.setattr("dqxclarity.translate.community.httpx.get", lambda *a, **k: _Resp())
    assert load_suppressions() == [("こわれた部分", "It broke.")]


# --------------------------------------------------------------------------------------------------
# LOCAL save -> load round-trip; missing file -> [] with NO network/exception (fast-startup contract)
# --------------------------------------------------------------------------------------------------


def test_save_load_suppressions_local_round_trip(tmp_path):
    entries = [("こわれた部分", "It broke."), ("<pc>はやられた", "<pc> was defeated.")]
    path = tmp_path / "suppressions.json"
    written = save_suppressions(path, entries)
    assert written == path
    # Read back EXACTLY what was written, as (ja, en) tuples.
    assert load_suppressions_local(path) == [
        ("こわれた部分", "It broke."),
        ("<pc>はやられた", "<pc> was defeated."),
    ]


def test_load_suppressions_local_missing_file_returns_empty_no_network(tmp_path, monkeypatch):
    # A missing local snapshot degrades to [] with NO exception and NO network call.
    def _no_net(*a, **k):
        raise AssertionError("load_suppressions_local must never hit the network")

    monkeypatch.setattr("dqxclarity.translate.community.httpx.get", _no_net)
    assert load_suppressions_local(tmp_path / "does_not_exist.json") == []


def test_load_suppressions_local_malformed_file_returns_empty(tmp_path):
    path = tmp_path / "suppressions.json"
    path.write_text("not valid json {", encoding="utf-8")
    # Unreadable/malformed -> [] (never raises).
    assert load_suppressions_local(path) == []


def test_save_suppressions_creates_parent_dirs(tmp_path):
    # The data dir may not exist yet on a fresh machine; save must create it.
    path = tmp_path / "nested" / "dir" / "suppressions.json"
    save_suppressions(path, [("あ", "a")])
    assert path.exists()
    assert load_suppressions_local(path) == [("あ", "a")]


def test_sync_persists_suppressions_local_round_trip(tmp_path, monkeypatch):
    # The `sync` command must fetch the suppressions over the network and WRITE the local snapshot
    # that `run` reads back. We mock the suppressions network fetch, neutralize the other (unrelated)
    # sync steps so they don't network, point the config-data dir at tmp_path, run sync, and assert
    # the suppressions file is written and re-loadable.
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cli.cfg_mod, "CONFIG_DIR", tmp_path)

    # Stub the heavy/unrelated sync steps to no-ops (they're imported into community and resolved at
    # call time inside cli.sync; patch them at the community source).
    monkeypatch.setattr("dqxclarity.translate.community.sync_community", lambda cache: 0)
    monkeypatch.setattr("dqxclarity.translate.community.sync_all_static", lambda cache: (0, 0))
    monkeypatch.setattr("dqxclarity.translate.community.sync_custom_supplements", lambda cache: 0)
    monkeypatch.setattr("dqxclarity.translate.glossary.sync_glossary", lambda cache_dir: 0)
    monkeypatch.setattr("dqxclarity.translate.community.fetch_reward_items", lambda **k: {})

    # The suppressions fetch returns the entries we expect persisted.
    monkeypatch.setattr(
        "dqxclarity.translate.community.fetch_suppressions",
        lambda **k: [("こわれた部分", "It broke.")],
    )

    cli.sync()

    snapshot = tmp_path / "suppressions.json"
    assert snapshot.exists()
    assert load_suppressions_local(snapshot) == [("こわれた部分", "It broke.")]


def test_sync_community_imports_bad_string_zero_fix_on_correct_key(tmp_path, monkeypatch):
    # A bad_string=0 FIX must land in the cache keyed on the CORRECT (column-5 original) ja, so a
    # later lookup by the real source string hits the curated fix.
    data = _make_merge_xlsx(
        {
            "Dialogue": [
                ["Japanese", "DeepL", "Fixed English", "Notes", "Original Bad String Text"],
                ["だれかの部分文", "x", "Someone's line.", "BAD STRING", "本当のソース文字列"],
                ["ふつうの行", "x", "Normal line.", None, None],
            ],
        }
    )

    class _Resp:
        content = data

        def raise_for_status(self):
            pass

    monkeypatch.setattr("dqxclarity.translate.community.httpx.get", lambda *a, **k: _Resp())
    cache = TranslationCache(tmp_path / "c.db")
    sync_community(cache)
    # The fix is keyed on the correct source, NOT the partial.
    assert cache.lookup("本当のソース文字列") == "Someone's line."
    assert cache.lookup("だれかの部分文") is None
    # The ordinary row still imports normally.
    assert cache.lookup("ふつうの行") == "Normal line."
    cache.close()


# --------------------------------------------------------------------------------------------------
# cli.run wiring — the suppression pre-pass is actually installed on the prose surfaces (#23).
#
# The pure pieces above prove build_translate_fn(..., suppression=idx) runs the pre-pass; this proves
# cli.run's hook-installation loop BUILDS a SuppressionIndex from load_suppressions() and passes it to
# every prose/text surface (the reviewer's critical finding: no SuppressionIndex was ever built or
# passed, so the pre-pass never ran at runtime). We drive the real cli.run with all externals mocked
# and assert the installed dialogue fn suppresses a known-broken input.
# --------------------------------------------------------------------------------------------------


from contextlib import contextmanager

from dqxclarity import cli
from dqxclarity import config as cfg_mod
from dqxclarity.process.hooks import HOOKS


def _run_capture_installed(monkeypatch, *, hook_names, suppressions):
    """Drive cli.run with mocked externals; return the captured (name, hook, fn) `installed` list.

    run() now reads the LOCAL snapshots (load_suppressions_local / load_reward_items_local) instead
    of doing any network — so the harness stubs the LOCAL readers (asserting nothing here touches the
    network) and records the path each was read from, proving run() points them at the config-data
    snapshot files (_suppressions_path() / _reward_items_path()).
    """
    monkeypatch.setattr(cli, "find_game_pid", lambda: 1234)
    monkeypatch.setattr(cli.hookjournal, "recover_orphans", lambda mem, pid: [])

    class _Mem:
        def __init__(self, pid):
            self.pid = pid

    monkeypatch.setattr("dqxclarity.process.memory_linux.LinuxProcessMemory", _Mem)

    class _Translator:
        def __init__(self):
            self.player_name_ja = self.player_name_en = ""
            self.sibling_name_ja = self.sibling_name_en = ""
            self.sync_provider = None
            self.cache = SimpleNamespace(close=lambda: None)

        def start(self):
            pass

        def stop(self):
            pass

        def lookup(self, key):
            return None  # force a cache miss so the suppression / MT ordering is observable

    monkeypatch.setattr(cli, "_build_translator", lambda cfg: _Translator())

    found = [SimpleNamespace(spec=HOOKS[n], func_addr=0x400000 + i)
             for i, n in enumerate(hook_names)]
    monkeypatch.setattr("dqxclarity.process.hooks.locate", lambda mem, names: found)
    monkeypatch.setattr(
        "dqxclarity.process.hooks.install",
        lambda mem, fh: SimpleNamespace(spec=fh.spec, restore=lambda *a, **k: None),
    )

    # The NETWORK fetchers must NEVER be called from run() any more.
    def _no_net(**k):
        raise AssertionError("run() must not call the network fetchers")

    monkeypatch.setattr("dqxclarity.translate.community.load_suppressions", _no_net)
    monkeypatch.setattr("dqxclarity.translate.community.load_reward_items", _no_net)

    # Local imports inside run() resolve from their source modules at call time -> patch the LOCAL
    # readers there, recording the path run() asks them to read so we can assert it's the snapshot.
    read_paths = {}

    def _local_supp(path):
        read_paths["suppressions"] = path
        return suppressions

    def _local_reward(path):
        read_paths["reward_items"] = path
        return {}

    monkeypatch.setattr(
        "dqxclarity.translate.community.load_suppressions_local", _local_supp
    )
    monkeypatch.setattr(
        "dqxclarity.translate.community.load_reward_items_local", _local_reward
    )

    @contextmanager
    def _fake_session(mem, pid, hooks, *, console):
        import threading
        yield threading.Event()

    monkeypatch.setattr(cli.hookjournal, "hook_session", _fake_session)

    captured = {}

    def _fake_serve(mem, installed, *, stop, game_gone=None, on_line=None):
        captured["installed"] = installed
        return 0

    monkeypatch.setattr("dqxclarity.runtime.dispatch.serve", _fake_serve)
    monkeypatch.setattr(cfg_mod, "load", lambda: cfg_mod.Config())

    # names/notice=False: this test only covers the suppression pre-pass on the hook surfaces, not the
    # polling name scanner (#30) or notice scanner (#27) — disable both so no real scanner thread spins
    # against the _Mem stub.
    cli.run(hooks=",".join(hook_names), duration=0.0, patch=False, names=False, notice=False)
    # run() must read the suppressions snapshot from the config-data dir.
    assert read_paths.get("suppressions") == cli._suppressions_path()
    return captured["installed"]


def test_cli_run_installs_suppression_prepass_on_prose_surface(monkeypatch):
    # A known-broken input ("こわれた文字列" contains the bad substring) must be suppressed by the
    # dialogue hook's installed fn — proving the SuppressionIndex was built and passed through.
    installed = _run_capture_installed(
        monkeypatch, hook_names=["dialogue"],
        suppressions=[("こわれた", "It broke.")],
    )
    fn = {name: fn for name, _h, fn in installed}["dialogue"]
    assert fn("こわれた文字列") == "It broke."  # substring suppression wins before cache/MT


def test_cli_run_missing_suppression_snapshot_degrades(monkeypatch):
    # A MISSING/empty local suppressions snapshot (no `sync` yet) must NOT abort run(); the pre-pass
    # is simply skipped (empty index). The local reader returns [] (never raises, never networks), so
    # a non-Japanese string still passes through and a Japanese miss is not spuriously suppressed.
    # This is the local-model analogue of the old "download failure degrades" test.
    monkeypatch.setattr(cli, "find_game_pid", lambda: 1234)
    monkeypatch.setattr(cli.hookjournal, "recover_orphans", lambda mem, pid: [])

    class _Mem:
        def __init__(self, pid):
            self.pid = pid

    monkeypatch.setattr("dqxclarity.process.memory_linux.LinuxProcessMemory", _Mem)

    class _Translator:
        def __init__(self):
            self.player_name_ja = self.player_name_en = ""
            self.sibling_name_ja = self.sibling_name_en = ""
            self.sync_provider = None
            self.cache = SimpleNamespace(close=lambda: None)

        def start(self):
            pass

        def stop(self):
            pass

        def lookup(self, key):
            return None

    monkeypatch.setattr(cli, "_build_translator", lambda cfg: _Translator())
    found = [SimpleNamespace(spec=HOOKS["dialogue"], func_addr=0x400000)]
    monkeypatch.setattr("dqxclarity.process.hooks.locate", lambda mem, names: found)
    monkeypatch.setattr(
        "dqxclarity.process.hooks.install",
        lambda mem, fh: SimpleNamespace(spec=fh.spec, restore=lambda *a, **k: None),
    )

    # The NETWORK fetchers must NEVER be called from run().
    def _no_net(**k):
        raise AssertionError("run() must not call the network fetchers")

    monkeypatch.setattr("dqxclarity.translate.community.load_suppressions", _no_net)
    monkeypatch.setattr("dqxclarity.translate.community.load_reward_items", _no_net)
    # Point the config-data dir at a fresh tmp so the snapshot files genuinely don't exist; the
    # REAL local readers run (no mock) and degrade to []/{} for the missing files.
    import tempfile
    from pathlib import Path as _Path

    tmpdir = _Path(tempfile.mkdtemp())
    monkeypatch.setattr(cli.cfg_mod, "CONFIG_DIR", tmpdir)

    @contextmanager
    def _fake_session(mem, pid, hooks, *, console):
        import threading
        yield threading.Event()

    monkeypatch.setattr(cli.hookjournal, "hook_session", _fake_session)
    captured = {}
    monkeypatch.setattr(
        "dqxclarity.runtime.dispatch.serve",
        lambda mem, installed, *, stop, game_gone=None, on_line=None:
            captured.__setitem__("installed", installed) or 0,
    )
    monkeypatch.setattr(cfg_mod, "load", lambda: cfg_mod.Config())

    # Must NOT raise despite the missing snapshot. names/notice=False keeps the polling scanners
    # (#30 names, #27 notice) out of this hook-installation test (no real thread against the _Mem stub).
    cli.run(hooks="dialogue", duration=0.0, patch=False, names=False, notice=False)
    fn = {name: fn for name, _h, fn in captured["installed"]}["dialogue"]
    # Pre-pass skipped (empty index): a non-Japanese string still short-circuits to None.
    assert fn("Already English") is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
