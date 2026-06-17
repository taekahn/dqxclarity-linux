"""Tests for #21 quest-reward per-field cleanup.

Covers ``translate.rewards.clean_quest_rewards`` (the pure, per-line mirror of upstream
clean_up_and_return_items):
  * item lookup hit (formatted ・{en}{pad}{qty});
  * quantity こ -> (N) and 他 -> (1), with skill-learn keywords -> "";
  * 討伐ポイント -> ・Experience Points + the points substring;
  * gender-prefix strip (男は / 女は, incl. fullwidth space);
  * multi-line join;
  * an unknown-item line falling back WITHOUT crashing.

Also covers the per-field routing pieces:
  * ``community.build_reward_items_dict`` merges items + key_items + custom_quest_rewards;
  * ``dispatch.build_rewards_translate_fn`` / ``FieldRouter`` route the reward fields only;
  * ``BlockingHook.serve_once`` applies the right fn per field index;
  * the quest HookSpec declares reward_field_indices=(3, 4) and the others declare none.
"""

from __future__ import annotations

import io
import json
import struct
import tarfile
import zipfile
from types import SimpleNamespace

import pytest

from dqxclarity.process.detour import STATE_DONE, STATE_REQUEST, BlockingHook
from dqxclarity.process.hooks import HOOKS
from dqxclarity.runtime.dispatch import (
    FieldRouter,
    build_quest_translate_fn,
    build_rewards_translate_fn,
)
from dqxclarity.translate.community import (
    build_reward_items_dict,
    load_reward_items_local,
    save_reward_items,
)
from dqxclarity.translate.db import TranslationCache
from dqxclarity.translate.pipeline import Translator
from dqxclarity.translate.rewards import clean_quest_rewards


# A small item dict standing in for the merged items/key_items/custom_quest_rewards dict.
ITEMS = {
    "ちいさなメダル": "Small Medal",
    "やくそう": "Medicinal Herb",
    "ぴかぴかコイン": "Gleaming Coin",
    "せかいじゅの葉": "Yggdrasil Leaf",
}


# --------------------------------------------------------------------------------------------------
# clean_quest_rewards — item lookup hit + formatting
# --------------------------------------------------------------------------------------------------


def test_item_lookup_hit_single_line_with_bullet():
    out = clean_quest_rewards("・やくそう", ITEMS)
    assert out.startswith("・Medicinal Herb")
    assert out.rstrip() == "・Medicinal Herb"  # no quantity -> trailing pad rstripped away


def test_item_lookup_hit_without_bullet():
    out = clean_quest_rewards("やくそう", ITEMS)
    assert not out.startswith("・")
    assert out.rstrip() == "Medicinal Herb"


# --------------------------------------------------------------------------------------------------
# quantity parsing: こ -> (N), 他 -> (1), skill-learn -> ""
# --------------------------------------------------------------------------------------------------


def test_quantity_ko_extracts_count():
    # "...３こ" -> quantity (3); fullwidth digit is NFKC-normalized to ascii.
    out = clean_quest_rewards("・ちいさなメダル　　３こ", ITEMS)
    assert out.startswith("・Small Medal")
    assert out.rstrip().endswith("(3)")


def test_quantity_ta_gives_one():
    # A line ending in 他 (and not a skill-learn) gets "(1)". Upstream parses the quantity from the
    # bulletless line but looks the item up AFTER stripping a trailing "　　…" annotation, so the
    # 他 sits behind a fullwidth-space delimiter and the name resolves cleanly.
    out = clean_quest_rewards("・やくそう　　他", ITEMS)
    assert out.rstrip().endswith("(1)")
    assert "Medicinal Herb" in out


def test_skill_learn_keyword_blanks_quantity():
    # A 他-ending line containing a skill-learn keyword must NOT get "(1)" — quantity is blank.
    # The item name itself isn't in the dict, so this is a multi-line case to exercise the join path.
    text = "・やくそう\n・必殺技を覚える他"
    out = clean_quest_rewards(text, ITEMS)
    lines = out.split("\n")
    assert lines[0].startswith("・Medicinal Herb")
    # The skill-learn line is unknown -> kept verbatim (no "(1)" appended).
    assert lines[1] == "・必殺技を覚える他"


# --------------------------------------------------------------------------------------------------
# 討伐ポイント -> Experience Points (single-line special case)
# --------------------------------------------------------------------------------------------------


def test_discovery_points_becomes_experience_points():
    # Upstream: a single-line miss containing 討伐ポイント returns "・Experience Points" + chars 6:18
    # of the bulletless line (the points substring).
    line = "・討伐ポイント１２３４５６"
    out = clean_quest_rewards(line, ITEMS)
    assert out.startswith("・Experience Points")
    # points substring = no_bullet[6:18] where no_bullet = "討伐ポイント１２３４５６".
    no_bullet = "討伐ポイント１２３４５６"
    assert out == "・Experience Points" + no_bullet[6:18]


# --------------------------------------------------------------------------------------------------
# gender prefix strip (男は / 女は, incl. fullwidth space)
# --------------------------------------------------------------------------------------------------


def test_gender_prefix_stripped_regular_space():
    out = clean_quest_rewards("男は やくそう", ITEMS)
    assert out.rstrip() == "Medicinal Herb"  # "男は " stripped, then resolved


def test_gender_prefix_stripped_fullwidth_space():
    out = clean_quest_rewards("女は　やくそう", ITEMS)
    assert out.rstrip() == "Medicinal Herb"  # fullwidth-space variant stripped too


# --------------------------------------------------------------------------------------------------
# multi-line join
# --------------------------------------------------------------------------------------------------


def test_multi_line_join_resolves_each():
    text = "・やくそう\n・せかいじゅの葉"
    out = clean_quest_rewards(text, ITEMS)
    lines = out.split("\n")
    assert len(lines) == 2
    assert lines[0].startswith("・Medicinal Herb")
    assert lines[1].startswith("・Yggdrasil Leaf")


def test_multi_line_mixes_known_and_unknown_without_crashing():
    text = "・やくそう\n・なぞのアイテム"  # second item not in dict
    out = clean_quest_rewards(text, ITEMS)
    lines = out.split("\n")
    assert lines[0].startswith("・Medicinal Herb")
    assert lines[1] == "・なぞのアイテム"  # unknown -> kept verbatim (no crash)


# --------------------------------------------------------------------------------------------------
# unknown-item fallback (single line) — returns original text, never crashes
# --------------------------------------------------------------------------------------------------


def test_unknown_single_line_returns_original():
    out = clean_quest_rewards("・なぞのアイテム", ITEMS)
    assert out == "・なぞのアイテム"  # single-line miss, no 討伐ポイント -> original text returned


def test_empty_text_does_not_crash():
    assert clean_quest_rewards("", ITEMS) == ""


# --------------------------------------------------------------------------------------------------
# community.build_reward_items_dict — merges the three sources (items + key_items + custom)
# --------------------------------------------------------------------------------------------------


def _b(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def _make_tarball(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _make_zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_build_reward_items_dict_merges_sources(monkeypatch):
    tarball = _make_tarball(
        {
            # items source (nested {id: {ja: en}})
            "r-main/json/_lang/en/subPackage05Client.json": _b({"1": {"やくそう": "Medicinal Herb"}}),
            # key_items source
            "r-main/json/_lang/en/subPackage41Client.win32.json": _b({"2": {"あおぞらのほこら": "Sky Shrine"}}),
            # an unrelated en file that must NOT be pulled into the item dict
            "r-main/json/_lang/en/eventTextSysQuestaClient.json": _b({"3": {"クエスト": "Quest"}}),
        }
    )
    custom = _make_zip(
        {
            "repo-main/json/custom_quest_rewards.json": _b({"4": {"ぴかぴかコイン": "Gleaming Coin"}}),
            # an unrelated custom json that must NOT be pulled into the item dict
            "repo-main/json/custom_corner_text.json": _b({"5": {"かんばん": "Sign"}}),
        }
    )

    class _Resp:
        def __init__(self, content):
            self.content = content

        def raise_for_status(self):
            pass

    def fake_get(url, *a, **k):
        return _Resp(custom if url.endswith(".zip") else tarball)

    monkeypatch.setattr("dqxclarity.translate.community.httpx.get", fake_get)
    d = build_reward_items_dict()
    assert d["やくそう"] == "Medicinal Herb"      # items
    assert d["あおぞらのほこら"] == "Sky Shrine"   # key_items
    assert d["ぴかぴかコイン"] == "Gleaming Coin"  # custom_quest_rewards
    assert "クエスト" not in d                      # unrelated quest source not pulled in
    assert "かんばん" not in d                       # unrelated custom source not pulled in


def test_build_reward_items_dict_custom_overrides_generic(monkeypatch):
    # custom_quest_rewards is merged LAST, so a curated reward name overrides the generic item name.
    tarball = _make_tarball(
        {"r-main/json/_lang/en/subPackage05Client.json": _b({"1": {"ほうび": "Generic Reward"}})}
    )
    custom = _make_zip(
        {"repo-main/json/custom_quest_rewards.json": _b({"2": {"ほうび": "Curated Reward"}})}
    )

    class _Resp:
        def __init__(self, content):
            self.content = content

        def raise_for_status(self):
            pass

    monkeypatch.setattr(
        "dqxclarity.translate.community.httpx.get",
        lambda url, *a, **k: _Resp(custom if url.endswith(".zip") else tarball),
    )
    d = build_reward_items_dict()
    assert d["ほうび"] == "Curated Reward"  # custom wins


# --------------------------------------------------------------------------------------------------
# LOCAL save -> load round-trip; missing file -> {} with NO network/exception (fast-startup contract)
# --------------------------------------------------------------------------------------------------


def test_save_load_reward_items_local_round_trip(tmp_path):
    items = {"やくそう": "Medicinal Herb", "ぴかぴかコイン": "Gleaming Coin"}
    path = tmp_path / "reward_items.json"
    written = save_reward_items(path, items)
    assert written == path
    assert load_reward_items_local(path) == items


def test_load_reward_items_local_missing_file_returns_empty_no_network(tmp_path, monkeypatch):
    def _no_net(*a, **k):
        raise AssertionError("load_reward_items_local must never hit the network")

    monkeypatch.setattr("dqxclarity.translate.community.httpx.get", _no_net)
    assert load_reward_items_local(tmp_path / "missing.json") == {}


def test_load_reward_items_local_malformed_file_returns_empty(tmp_path):
    path = tmp_path / "reward_items.json"
    path.write_text("definitely not json", encoding="utf-8")
    assert load_reward_items_local(path) == {}


def test_save_reward_items_creates_parent_dirs(tmp_path):
    path = tmp_path / "nested" / "reward_items.json"
    save_reward_items(path, {"あ": "a"})
    assert path.exists()
    assert load_reward_items_local(path) == {"あ": "a"}


def test_sync_persists_reward_items_local_round_trip(tmp_path, monkeypatch):
    # The `sync` command must build the reward dict over the network and WRITE the local snapshot
    # that `run` reads back. We mock the reward fetch via the real build path (httpx.get -> tarball +
    # custom zip), neutralize the other (unrelated) sync steps so they don't network, point the
    # config-data dir at tmp_path, run sync, and assert the snapshot is written and re-loadable.
    from dqxclarity import cli
    from dqxclarity import config as cfg_mod

    tarball = _make_tarball(
        {
            "r-main/json/_lang/en/subPackage05Client.json": _b({"1": {"やくそう": "Medicinal Herb"}}),
            "r-main/json/_lang/en/subPackage41Client.win32.json": _b(
                {"2": {"あおぞらのほこら": "Sky Shrine"}}
            ),
        }
    )
    custom = _make_zip(
        {"repo-main/json/custom_quest_rewards.json": _b({"4": {"ぴかぴかコイン": "Gleaming Coin"}})}
    )

    class _Resp:
        def __init__(self, content):
            self.content = content

        def raise_for_status(self):
            pass

    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cli.cfg_mod, "CONFIG_DIR", tmp_path)

    # Neutralize the heavy/unrelated sync steps so only the reward fetch+save runs for real.
    monkeypatch.setattr("dqxclarity.translate.community.sync_community", lambda cache: 0)
    monkeypatch.setattr("dqxclarity.translate.community.sync_all_static", lambda cache: (0, 0))
    monkeypatch.setattr("dqxclarity.translate.community.sync_custom_supplements", lambda cache: 0)
    monkeypatch.setattr("dqxclarity.translate.glossary.sync_glossary", lambda cache_dir: 0)
    monkeypatch.setattr("dqxclarity.translate.community.fetch_suppressions", lambda **k: [])

    # The reward fetch (fetch_reward_items -> build_reward_items_dict) downloads the tarball + custom
    # zip via community.httpx.get; serve the fixtures by URL suffix.
    monkeypatch.setattr(
        "dqxclarity.translate.community.httpx.get",
        lambda url, *a, **k: _Resp(custom if url.endswith(".zip") else tarball),
    )

    cli.sync()

    snapshot = tmp_path / "reward_items.json"
    assert snapshot.exists()
    d = load_reward_items_local(snapshot)
    assert d["やくそう"] == "Medicinal Herb"
    assert d["あおぞらのほこら"] == "Sky Shrine"
    assert d["ぴかぴかコイン"] == "Gleaming Coin"


# --------------------------------------------------------------------------------------------------
# dispatch.build_rewards_translate_fn + FieldRouter
# --------------------------------------------------------------------------------------------------


def _cfg(**over):
    tr = SimpleNamespace(
        player_name_ja="", player_name_en="", sibling_name_ja="", sibling_name_en="",
        wrap_width=46, lines_per_page=0,
    )
    for k, v in over.items():
        setattr(tr, k, v)
    return SimpleNamespace(translate=tr)


def test_rewards_translate_fn_cleans_and_returns_none_on_noop():
    fn = build_rewards_translate_fn(ITEMS)
    assert fn("・やくそう").startswith("・Medicinal Herb")
    assert fn("Already English") is None        # non-Japanese -> None (leave as-is)
    assert fn("・なぞのアイテム") is None         # single-line miss -> cleaned == ja -> None (no-op)


def test_field_router_routes_only_overridden_indices():
    default_fn = lambda ja: "DEFAULT"
    reward_fn = lambda ja: "REWARD"
    router = FieldRouter(default_fn, {3: reward_fn, 4: reward_fn})
    assert router.fn_for(0) is default_fn
    assert router.fn_for(2) is default_fn
    assert router.fn_for(3) is reward_fn
    assert router.fn_for(4) is reward_fn
    assert router("x") == "DEFAULT"  # callable falls back to default


def test_build_quest_translate_fn_returns_router_with_reward_fields(tmp_path):
    cache = TranslationCache(tmp_path / "q.db")
    translator = Translator(cache)
    router = build_quest_translate_fn(
        _cfg(), translator, reward_field_indices=(3, 4), items_dict=ITEMS,
        wrap_width=46, lines_per_page=0, sync=False,
    )
    assert isinstance(router, FieldRouter)
    # The reward fields (3, 4) use the reward fn; another field uses the default (prose) fn.
    assert router.fn_for(3)("・やくそう").startswith("・Medicinal Herb")
    assert router.fn_for(0) is router.default_fn
    cache.close()


# --------------------------------------------------------------------------------------------------
# BlockingHook.serve_once applies the right fn per field index (per-field routing end to end)
# --------------------------------------------------------------------------------------------------


STATE = 0x10
SLOT = 0x14
BASE = 0x1000


class FakeMem:
    def __init__(self) -> None:
        self.u32: dict[int, int] = {STATE: STATE_REQUEST, SLOT: BASE}
        self.buffers: dict[int, bytes] = {}
        self.writes: list[tuple[int, bytes]] = []

    def read_u32(self, addr: int) -> int:
        return self.u32.get(addr, 0)

    def read(self, addr: int, size: int) -> bytes:
        return self.buffers.get(addr, b"")[:size]

    def write(self, addr: int, data: bytes) -> None:
        self.writes.append((addr, bytes(data)))
        if addr == STATE:
            self.u32[STATE] = struct.unpack("<I", data[:4])[0]


def test_serve_once_per_field_routing_uses_reward_fn_for_reward_field():
    # Two fields: index 0 (prose) and index 1 (reward). A FieldRouter sends the reward field through
    # the reward fn and the prose field through the default fn; serve_once must honour fn_for(index).
    mem = FakeMem()
    mem.buffers[BASE + 0] = "クエスト名".encode() + b"\x00" + b"\x00" * 200
    mem.buffers[BASE + 100] = "・やくそう".encode() + b"\x00" + b"\x00" * 200
    hook = BlockingHook(0x400000, 0, STATE, SLOT, 0, b"", fields=((0, 90), (100, 90)))

    default_fn = lambda ja: "PROSE-EN"
    reward_fn = build_rewards_translate_fn(ITEMS)
    router = FieldRouter(default_fn, {1: reward_fn})

    hook.serve_once(mem, router)

    prose_writes = [d for a, d in mem.writes if a == BASE + 0]
    reward_writes = [d for a, d in mem.writes if a == BASE + 100]
    assert prose_writes and prose_writes[-1].startswith(b"PROSE-EN\x00")
    assert reward_writes and reward_writes[-1].startswith("・Medicinal Herb".encode())
    # Always releases the game thread.
    assert [d for a, d in mem.writes if a == STATE][-1] == struct.pack("<I", STATE_DONE)


def test_serve_once_plain_callable_still_applies_to_all_fields():
    # Backward compatibility: a plain callable (no fn_for) applies to EVERY field, unchanged.
    mem = FakeMem()
    mem.buffers[BASE + 0] = "あ".encode() + b"\x00" + b"\x00" * 50
    mem.buffers[BASE + 100] = "い".encode() + b"\x00" + b"\x00" * 50
    hook = BlockingHook(0x400000, 0, STATE, SLOT, 0, b"", fields=((0, 80), (100, 80)))
    hook.serve_once(mem, lambda j: "X" if j == "あ" else "Y")
    assert [d for a, d in mem.writes if a == BASE + 0][-1].startswith(b"X\x00")
    assert [d for a, d in mem.writes if a == BASE + 100][-1].startswith(b"Y\x00")


# --------------------------------------------------------------------------------------------------
# HookSpec declares reward_field_indices on quest ONLY (other hooks unaffected)
# --------------------------------------------------------------------------------------------------


def test_quest_spec_declares_reward_field_indices():
    q = HOOKS["quest"]
    assert q.reward_field_indices == (3, 4)
    # Indices 3 and 4 map to the reward offsets 640 (questRewards) and 744 (questRepeatRewards).
    assert q.fields[3][0] == 640
    assert q.fields[4][0] == 744


def test_other_hooks_have_no_reward_field_indices():
    for name, spec in HOOKS.items():
        if name == "quest":
            continue
        assert spec.reward_field_indices == (), f"{name} unexpectedly declares reward fields"


# --------------------------------------------------------------------------------------------------
# cli.run wiring — the quest hook actually gets a FieldRouter built from the reward dict (#21).
#
# The pure pieces above prove build_quest_translate_fn/FieldRouter behave; this proves cli.run's
# hook-installation loop INSTALLS that path for the quest hook (the reviewer's critical finding was
# that it fell through to the plain `else` branch and never built a router). We drive the real
# cli.run with every external (memory, hook locate/install/session, serve, translator, network
# downloads) mocked, and capture the (name, hook, fn) triples the serve loop receives.
# --------------------------------------------------------------------------------------------------


from contextlib import contextmanager

from dqxclarity import cli
from dqxclarity import config as cfg_mod


def _run_capture_installed(monkeypatch, *, hook_names, reward_items=None, suppressions=None):
    """Drive cli.run with mocked externals; return the captured `installed` (name, hook, fn) list.

    Every surface cli.run touches is stubbed so nothing hits a real game/network: find_game_pid,
    LinuxProcessMemory, hookjournal.recover_orphans/hook_session, the translator builder, the hook
    locate/install. run() now reads the LOCAL snapshots (load_reward_items_local /
    load_suppressions_local) instead of downloading, so the harness stubs those LOCAL readers,
    asserts the NETWORK fetchers are never called, and counts local-reader invocations in
    ``reward_calls`` (the reward snapshot is only read when a reward hook is installed). `serve` is
    replaced with a capture that records the installed triples and returns immediately.
    """
    monkeypatch.setattr(cli, "find_game_pid", lambda: 1234)
    monkeypatch.setattr(cli.hookjournal, "recover_orphans", lambda mem, pid: [])

    class _Mem:
        def __init__(self, pid):
            self.pid = pid

    monkeypatch.setattr(
        "dqxclarity.process.memory_linux.LinuxProcessMemory", _Mem
    )

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

    found = [SimpleNamespace(spec=HOOKS[n], func_addr=0x400000 + i)
             for i, n in enumerate(hook_names)]
    monkeypatch.setattr("dqxclarity.process.hooks.locate", lambda mem, names: found)
    monkeypatch.setattr(
        "dqxclarity.process.hooks.install",
        lambda mem, fh: SimpleNamespace(spec=fh.spec, restore=lambda *a, **k: None),
    )

    # The NETWORK fetchers must NEVER be called from run() now.
    def _no_net(**k):
        raise AssertionError("run() must not call the network fetchers")

    monkeypatch.setattr("dqxclarity.translate.community.load_reward_items", _no_net)
    monkeypatch.setattr("dqxclarity.translate.community.load_suppressions", _no_net)

    # The LOCAL readers -> in-memory fixtures. ``reward_items`` may be an Exception fixture to model a
    # malformed snapshot; but the REAL local reader never raises, so we emulate "no usable data" by
    # returning {} in that case (a malformed file degrades to {}). The read path is recorded so we can
    # assert run() points the reader at the config-data snapshot.
    reward_calls = {"n": 0}
    read_paths = {}
    if reward_items is None:
        reward_items = dict(ITEMS)

    def _load_reward_items_local(path):
        reward_calls["n"] += 1
        read_paths["reward_items"] = path
        if isinstance(reward_items, Exception):
            return {}  # malformed/missing snapshot -> {} (the real reader never raises)
        return reward_items

    if suppressions is None:
        suppressions = []

    def _load_suppressions_local(path):
        read_paths["suppressions"] = path
        return suppressions

    # cli.run does a LOCAL `from .translate.community import load_reward_items_local,
    # load_suppressions_local` inside the function body, so the names resolve from the community
    # module at call time — patch them there (patching cli.* would miss the local rebind).
    monkeypatch.setattr(
        "dqxclarity.translate.community.load_reward_items_local", _load_reward_items_local
    )
    monkeypatch.setattr(
        "dqxclarity.translate.community.load_suppressions_local", _load_suppressions_local
    )

    @contextmanager
    def _fake_session(mem, pid, hooks, *, console):
        import threading
        yield threading.Event()

    monkeypatch.setattr(cli.hookjournal, "hook_session", _fake_session)

    captured = {}

    def _fake_serve(mem, installed, *, stop, game_gone=None, on_line=None, profiler=None):
        captured["installed"] = installed
        return 0

    # `serve` is also a local import in run() (from .runtime.dispatch import ..., serve) -> patch at
    # source so the local name resolves to the capture.
    monkeypatch.setattr("dqxclarity.runtime.dispatch.serve", _fake_serve)
    monkeypatch.setattr(cfg_mod, "load", lambda: cfg_mod.Config())

    # names/notice=False: this is a reward-field hook test, not a name-scanner (#30) or notice-scanner
    # (#27) test — keep both polling scanners off so no real thread runs against the _Mem stub.
    cli.run(hooks=",".join(hook_names), duration=0.0, patch=False, names=False, notice=False)
    captured["reward_calls"] = reward_calls["n"]
    captured["read_paths"] = read_paths
    return captured


def test_cli_run_installs_field_router_for_quest_hook(monkeypatch):
    cap = _run_capture_installed(monkeypatch, hook_names=["quest"])
    fns = {name: fn for name, _hook, fn in cap["installed"]}
    assert "quest" in fns
    router = fns["quest"]
    # The reviewer's critical bug: the quest hook fell into the plain `else` and got a bare
    # translate fn. With the fix it MUST be a FieldRouter wired to the reward dict.
    assert isinstance(router, FieldRouter)
    # The reward field (index 3) routes through the reward-cleanup fn built from the loaded dict.
    assert router.fn_for(3)("・やくそう").startswith("・Medicinal Herb")
    # A prose field uses the default whole-string fn, NOT the reward fn.
    assert router.fn_for(0) is router.default_fn
    # The reward dict was read from the LOCAL snapshot (no network) exactly once — the quest hook
    # declares reward fields — and from the config-data snapshot path.
    assert cap["reward_calls"] == 1
    assert cap["read_paths"]["reward_items"] == cli._reward_items_path()


def test_cli_run_missing_reward_snapshot_degrades_to_empty(monkeypatch):
    # A MISSING/malformed local reward snapshot (no `sync` yet) must NOT abort run(); the router is
    # still built, just with an empty dict (every reward line falls back to the prose path / passes
    # through). The local reader degrades to {} (never raises, never networks). This is the
    # local-model analogue of the old "download failure degrades" test.
    cap = _run_capture_installed(
        monkeypatch, hook_names=["quest"], reward_items=RuntimeError("malformed snapshot")
    )
    router = {name: fn for name, _h, fn in cap["installed"]}["quest"]
    assert isinstance(router, FieldRouter)
    # With no items, a single-line reward field cleans to nothing usable -> reward fn returns None.
    assert router.fn_for(3)("・やくそう") is None


def test_cli_run_skips_reward_read_when_quest_not_hooked(monkeypatch):
    # load_reward_items_local is only consulted when a hook declaring reward_field_indices is
    # installed. dialogue declares no reward fields, so the loop's `any(... reward_field_indices ...)`
    # guard is False and the local read is skipped entirely (the harness counts reader invocations).
    cap = _run_capture_installed(monkeypatch, hook_names=["dialogue"])
    assert cap["reward_calls"] == 0
    assert all(HOOKS[name].reward_field_indices == () for name, _h, _f in cap["installed"])


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
