"""Tests for the translation layer (cache, romanizer, provider parsing, pipeline worker)."""

from __future__ import annotations

import time

from dqxclarity.translate.db import TranslationCache
from dqxclarity.translate.pipeline import Translator
from dqxclarity.translate.providers.claude_cli import ClaudeCliProvider
from dqxclarity.translate.romanize import romanize


def test_cache_roundtrip_and_persistence(tmp_path):
    p = tmp_path / "cache.db"
    c = TranslationCache(p)
    assert c.lookup("こんにちは") is None
    c.store("こんにちは", "Hello", "test")
    assert c.lookup("こんにちは") == "Hello"
    c.store_many([("ありがとう", "Thanks", "test"), ("はい", "Yes", "test")])
    assert len(c) == 3
    c.close()
    # Reopen: hot cache rehydrates from disk.
    c2 = TranslationCache(p)
    assert c2.lookup("ありがとう") == "Thanks"
    assert len(c2) == 3
    c2.close()


def test_romanize_basic():
    assert romanize("たろう") == "Tarou"
    assert romanize("") == ""


def test_claude_cli_parse_envelope_and_fence():
    # Plain envelope with a JSON-array result.
    env = '{"type":"result","result":"[\\"Hello\\", \\"World\\"]"}'
    assert ClaudeCliProvider._parse(env, 2) == ["Hello", "World"]
    # Result wrapped in a ```json fence.
    fenced = '{"result":"```json\\n[\\"A\\", \\"B\\"]\\n```"}'
    assert ClaudeCliProvider._parse(fenced, 2) == ["A", "B"]
    # Length mismatch -> all None.
    assert ClaudeCliProvider._parse('{"result":"[\\"only one\\"]"}', 2) == [None, None]
    # Garbage -> all None.
    assert ClaudeCliProvider._parse("not json", 1) == [None]


def test_pipeline_name_path(tmp_path):
    c = TranslationCache(tmp_path / "c.db")
    t = Translator(c, romanize_names=True)
    assert t.translate_name("たろう") == "Tarou"
    assert c.lookup("たろう") == "Tarou"  # cached
    assert t.translate_name("たろう") == "Tarou"  # served from cache
    c.close()


class _FakeProvider:
    name = "claude_cli"  # rank 2, so it upgrades rank-1 entries

    def available(self):
        return True

    def translate(self, texts):
        return [t.upper() for t in texts]


def test_two_tier_sync_then_upgrade(tmp_path):
    c = TranslationCache(tmp_path / "c.db")
    fast = _FakeProvider()
    fast.name = "googletranslatefree"  # rank 1
    fast.translate = lambda texts: [t + "-fast" for t in texts]
    t = Translator(c, sync_provider=fast, upgrade_provider=_FakeProvider(), batch_size=8)
    t.start()
    # First view: fast synchronous translation, and queues a background upgrade.
    assert t.translate_now("abc") == "abc-fast"
    assert c.source_of("abc") == "googletranslatefree"
    # Background upgrade replaces it with the higher-quality (uppercased) version.
    deadline = time.time() + 3
    while time.time() < deadline and c.source_of("abc") != "claude_cli":
        time.sleep(0.05)
    t.stop()
    assert c.lookup("abc") == "ABC"  # upgraded
    assert c.source_of("abc") == "claude_cli"


def test_async_request_translates_via_sync_provider_when_no_upgrade(tmp_path):
    # REGRESSION: an async surface (sync=False, e.g. the quest menu) queues request() for the
    # background worker. With the upgrade (quality) provider OFF, the worker must fall back to the
    # SYNC provider so uncovered async text still gets translated — otherwise it stays Japanese
    # forever (rank_of(None)==1==rank_of(googletranslatefree) made _wants_upgrade wrongly skip it).
    c = TranslationCache(tmp_path / "a.db")
    fast = _FakeProvider()
    fast.name = "googletranslatefree"  # rank 1
    fast.translate = lambda texts: [t + "-bg" for t in texts]
    t = Translator(c, sync_provider=fast, upgrade_provider=None)  # upgrade provider OFF
    t.start()
    assert c.source_of("uncovered") is None
    t.request("uncovered")  # what the async (sync=False) path does for an uncovered string
    deadline = time.time() + 3
    while time.time() < deadline and c.lookup("uncovered") is None:
        time.sleep(0.05)
    t.stop()
    assert c.lookup("uncovered") == "uncovered-bg"  # background-translated via the sync provider
    assert c.source_of("uncovered") == "googletranslatefree"


def test_no_downgrade(tmp_path):
    c = TranslationCache(tmp_path / "c.db")
    c.store("x", "human", "community")  # rank 3
    assert c.store_if_better("x", "google", "googletranslatefree") is False
    assert c.lookup("x") == "human"  # not downgraded


def test_store_if_better_equal_rank_overwrites(tmp_path):
    c = TranslationCache(tmp_path / "e.db")
    c.store("x", "v1", "googletranslatefree")  # rank 1
    assert c.store_if_better("x", "v2", "google") is True  # equal rank -> overwrites
    assert c.lookup("x") == "v2"
    c.close()


def test_cache_concurrent_stores_stay_consistent(tmp_path):
    import threading

    path = tmp_path / "cc.db"
    c = TranslationCache(path)

    def worker(n):
        for i in range(200):
            c.store_if_better(f"k{i}", f"v{n}-{i}", "googletranslatefree")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(c) == 200  # no lost/duplicated keys
    c.close()
    # In-memory and on-disk agree after concurrent writes.
    c2 = TranslationCache(path)
    assert len(c2) == 200
    c2.close()


def test_translate_conversation_sync_translates_inline(tmp_path):
    from dqxclarity.translate.dialogue import translate_conversation

    class FastProvider:
        name = "googletranslatefree"

        def available(self):
            return True

        def translate(self, texts):
            return [t + "-EN" for t in texts]

    c = TranslationCache(tmp_path / "s.db")
    t = Translator(c, sync_provider=FastProvider())
    out = translate_conversation(t, "「こんにちは。", sync=True)  # miss -> translated inline
    assert out is not None and "-EN" in out and "「" not in out
    # async path on an uncached string returns None (queued) on FIRST view, not a partial render —
    # then (strengthened) the background worker actually fills the cache so a later view resolves.
    c2 = TranslationCache(tmp_path / "s2.db")
    t2 = Translator(c2, sync_provider=FastProvider())  # provider present -> the queue gets drained
    t2.start()
    ja2 = "「べつのせりふ。"
    assert translate_conversation(t2, ja2, sync=False) is None  # first view: queued, renders nothing
    from dqxclarity.translate.wrap import normalize_source

    norm2 = normalize_source(ja2)
    deadline = time.time() + 3
    while time.time() < deadline and c2.lookup(norm2) is None:
        time.sleep(0.05)
    t2.stop()
    assert c2.lookup(norm2) is not None  # the async request really translated in the background
    assert translate_conversation(t2, ja2, sync=False) is not None  # now resolves from cache
    c.close()
    c2.close()


def test_wrap_dialogue():
    from dqxclarity.translate.wrap import add_page_breaks, normalize_source, wrap_dialogue

    assert normalize_source("Hello<br>world\nfoo") == "Hello world foo"
    text = "This is a fairly long line of English dialogue that should wrap across " \
           "several lines so we can verify page breaks get inserted correctly every three."
    out = wrap_dialogue(text, width=46)
    lines = [ln for ln in out.split("\n") if ln != "<br>"]
    assert all(len(ln) <= 46 for ln in lines)  # wrapped to width
    assert "<br>" in out  # paginated
    # <br> appears after every 3 content lines
    five = add_page_breaks("a\nb\nc\nd\ne")
    assert five.split("\n") == ["a", "b", "c", "<br>", "d", "e"]


def test_dialogue_preserves_control_tags(tmp_path):
    from dqxclarity.translate.dialogue import translate_conversation
    from dqxclarity.translate.wrap import normalize_source

    c = TranslationCache(tmp_path / "d.db")
    c.store(normalize_source("「テストの　メッセージだよ。"), "This is a test message.", "x")
    t = Translator(c)
    out = translate_conversation(t, "「テストの　メッセージだよ。<wait=1000><close>")
    assert out == "This is a test message.<wait=1000><close>"  # terminators kept, 「 stripped
    # Missing translation -> None (queued), not a half-rendered string.
    assert translate_conversation(t, "「べつの　セリフ。<close>") is None
    c.close()


def test_dialogue_select_menu(tmp_path):
    from dqxclarity.translate.dialogue import translate_conversation
    from dqxclarity.translate.wrap import normalize_source

    c = TranslationCache(tmp_path / "s.db")
    c.store(normalize_source("もう一度　戦いますか？"), "Battle again?", "x")
    c.store(normalize_source("旅の扉に入る"), "Enter Teleportal", "x")
    c.store(normalize_source("やめる"), "Quit", "x")
    t = Translator(c)
    ja = (
        "もう一度　戦いますか？\n<select>\n旅の扉に入る\nやめる\n<select_end>\n"
        "<case 1>\n<break>\n<case_cancel>\n<break>\n<case_end>"
    )
    out = translate_conversation(t, ja)
    # options stay on their own lines (unwrapped), scaffold preserved
    assert "<select>\nEnter Teleportal\nQuit\n<select_end>" in out
    assert "<case 1>" in out and "<break>" in out and "<case_end>" in out
    assert out.startswith("Battle again?")
    c.close()


def test_community_placeholder_round_trip(tmp_path):
    from dqxclarity.translate.placeholders import from_placeholders, to_placeholders

    c = TranslationCache(tmp_path / "p.db")
    # community entry stored with the player-name placeholder
    c.store("<pnplacehold>たちは　成功した！", "<pnplacehold> and co. succeeded!", "community")
    t = Translator(c)
    mem_capture = "タイカンたちは　成功した！"  # what memory shows for player タイカン
    key = to_placeholders(mem_capture, "タイカン", "")
    en = t.lookup(key)
    assert from_placeholders(en, "Taikan", "") == "Taikan and co. succeeded!"
    c.close()


def test_dialogue_tag_only_passthrough(tmp_path):
    from dqxclarity.translate.dialogue import translate_conversation

    c = TranslationCache(tmp_path / "t.db")
    t = Translator(c)
    # no translatable Japanese -> tags pass through unchanged (no MT queued)
    assert translate_conversation(t, "<speed=0><close>") == "<speed=0><close>"
    c.close()


def test_request_noop_without_provider(tmp_path):
    # With NO provider at all there is genuinely nothing to translate, so request() is a real no-op.
    # Strengthened beyond "must not raise": assert the OBSERVABLE no-op — nothing is queued, start()
    # spins up no worker, and the string stays untranslated even after the worker would have run.
    c = TranslationCache(tmp_path / "c.db")
    t = Translator(c)  # no sync_provider, no upgrade_provider
    assert t._background_provider is None  # nothing for the worker to use
    t.request("xyz")  # must not raise or block (no provider)
    assert t._q.empty()  # _wants_upgrade is False with no provider -> nothing enqueued
    t.start()  # no background provider -> start() is a no-op (no thread)
    assert t._worker is None
    t.request("xyz")
    time.sleep(0.05)  # give any (non-existent) worker a chance to wrongly translate
    t.stop()
    assert c.lookup("xyz") is None  # still untranslated — provably a no-op, not just "didn't raise"
    c.close()
