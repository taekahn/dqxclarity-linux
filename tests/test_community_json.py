"""Tests for the static translation JSON parser (translate/community.parse_translation_json).

These cover both shapes the dqx-translation-project sources use — nested ``{id:{ja:en}}`` and flat
``{ja:en}`` — plus the skip rules (empty/null English, English identical to Japanese, untranslated
placeholders) and robustness to unexpected shapes. All fixtures are tiny in-memory JSON; nothing
here touches the network.
"""

from __future__ import annotations

import io
import json
import tarfile

from dqxclarity.translate.community import parse_translation_json


def _b(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def _make_tarball(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def test_nested_shape_quest_file():
    # The quest file shape: {"<id>": {"<japanese>": "<english>"}}.
    data = _b(
        {
            "10001": {"コルット地方で新種発見？": "New Species in Colt Region?"},
            "10002": {"さまよう霊魂": "The Wandering Spirit"},
        }
    )
    rows = parse_translation_json(data)
    assert ("コルット地方で新種発見？", "New Species in Colt Region?") in rows
    assert ("さまよう霊魂", "The Wandering Spirit") in rows
    assert len(rows) == 2


def test_flat_shape_ja_to_en():
    # The flat shape: {"<japanese>": "<english>"}.
    data = _b({"スライム": "Slime", "ドラキー": "Dracky"})
    rows = parse_translation_json(data)
    assert ("スライム", "Slime") in rows
    assert ("ドラキー", "Dracky") in rows
    assert len(rows) == 2


def test_skips_empty_null_and_identical():
    data = _b(
        {
            "あ": "",  # empty English
            "い": None,  # null English (nested would be non-str; here top-level non-str)
            "う": "う",  # English identical to Japanese -> still untranslated
            "え": "E",  # good
        }
    )
    rows = parse_translation_json(data)
    assert rows == [("え", "E")]


def test_skips_placeholder_markers():
    data = _b(
        {
            "1": {"魔王": "untranslated"},
            "2": {"勇者": "Hero"},
            "3": {"村人": "No Translation"},
        }
    )
    rows = parse_translation_json(data)
    assert rows == [("勇者", "Hero")]


def test_nested_skips_empty_and_identical():
    data = _b(
        {
            "a": {"火": ""},  # empty
            "b": {"水": "水"},  # identical
            "c": {"風": "Wind"},  # good
        }
    )
    rows = parse_translation_json(data)
    assert rows == [("風", "Wind")]


def test_unexpected_shapes_are_skipped_not_raised():
    # Non-dict top level -> empty.
    assert parse_translation_json(_b(["not", "a", "dict"])) == []
    assert parse_translation_json(_b("just a string")) == []
    # Mixed: list/number/None values are skipped; good entries survive.
    data = _b(
        {
            "list": ["x"],
            "num": 5,
            "none": None,
            "good_flat": "Good",
            "good_nested": {"鍵": "Key"},
        }
    )
    rows = parse_translation_json(data)
    assert ("good_flat", "Good") in rows
    assert ("鍵", "Key") in rows
    assert len(rows) == 2


def test_nested_with_non_str_inner_members_skipped():
    data = _b({"1": {"剣": None, "盾": "Shield", "槍": 7}})
    rows = parse_translation_json(data)
    assert rows == [("盾", "Shield")]


def test_invalid_json_returns_empty():
    assert parse_translation_json(b"{not valid json") == []
    assert parse_translation_json(b"") == []


def test_import_translation_tarball(tmp_path):
    # Import only json/_lang/en/*.json members; ignore other langs and non-json files.
    from dqxclarity.translate.community import import_translation_tarball
    from dqxclarity.translate.db import TranslationCache

    tb = _make_tarball(
        {
            "dqx_translations-main/json/_lang/en/eventTextSysQuestaClient.json": _b(
                {"1": {"コルット地方で新種発見？": "New Species in Colt Region?"}}
            ),
            "dqx_translations-main/json/_lang/en/subPackage05Client.json": _b(
                {"スライム": "Slime", "あ": ""}  # empty-EN entry skipped
            ),
            "dqx_translations-main/json/_lang/de/foo.json": _b({"x": "y"}),  # wrong lang -> ignored
            "dqx_translations-main/README.md": b"readme",  # non-json -> ignored
        }
    )
    cache = TranslationCache(tmp_path / "c.db")
    files, rows = import_translation_tarball(tb, cache)
    assert files == 2 and rows == 2  # 2 en files, 2 usable rows (empty skipped)
    assert cache.lookup("コルット地方で新種発見？") == "New Species in Colt Region?"
    assert cache.lookup("スライム") == "Slime"
    assert cache.lookup("x") is None  # the de/ file was not imported
    cache.close()


def test_sync_custom_supplements(tmp_path, monkeypatch):
    # sync_custom_supplements now fetches the whole dqx-custom-translations repo archive zip ONCE
    # and globs every */json/*.json member (see import_custom_zip). The monkeypatched httpx.get
    # returns one response whose content is a real in-memory ZIP archive; json members import and
    # non-json members are ignored. (Mirrors the zip fixture used in tests/test_data_gaps.py.)
    import zipfile

    from dqxclarity.translate import community
    from dqxclarity.translate.db import TranslationCache

    class _Resp:
        def __init__(self, content: bytes):
            self.content = content

        def raise_for_status(self):
            pass

    def _make_zip(files: dict[str, bytes]) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            for name, content in files.items():
                zf.writestr(name, content)
        return buf.getvalue()

    zip_bytes = _make_zip(
        {
            "dqx-custom-translations-main/json/_lang/en/custom_quest_rewards.json": _b(
                {"褒美": "Reward"}
            ),
            "dqx-custom-translations-main/json/custom_team_quests.json": _b(
                {"団クエ": "Team Quest"}
            ),
            # A non-json member that must be ignored by the */json/*.json glob.
            "dqx-custom-translations-main/csv/merge.xlsx": b"not json",
        }
    )

    def fake_get(url, **kwargs):
        # Exactly one fetch of the whole-repo archive — never a per-file GET.
        assert url == community.CUSTOM_TRANSLATIONS_ZIP_URL
        return _Resp(zip_bytes)

    monkeypatch.setattr(community.httpx, "get", fake_get)
    cache = TranslationCache(tmp_path / "c.db")
    # Two json members import; the non-json csv member is ignored.
    assert community.sync_custom_supplements(cache) == 2
    assert cache.lookup("褒美") == "Reward"
    assert cache.lookup("団クエ") == "Team Quest"
    cache.close()
