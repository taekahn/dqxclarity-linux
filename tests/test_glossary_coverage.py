"""Coverage for the glossary download / snapshot / parse edge paths (translate/glossary.py).

The existing tests/test_glossary.py covers the substitution contract (longest-first, meta chars,
no-op-when-empty) and a basic save/load round-trip. This file adds the thin download/snapshot edges
that were uncovered:

* extract_glossary_from_zip pulls glossary.csv out of an in-memory repo archive (both the ``/csv/``
  and ``/generate_glossary/`` locations the regex accepts), and degrades to ``[]`` when absent;
* load_glossary's three-step resolution order — prefer a local snapshot, else download (mocked) and
  persist, else (offline / no snapshot / download=False) an EMPTY no-op glossary;
* sync_glossary downloads (mocked), writes a snapshot, and returns the term count;
* save_glossary + parse_glossary_csv round-trip including the first-comma-split EN-with-comma case;
* a compiled matcher built from many keys (with regex-meta chars) substitutes longest-first in one
  pass and stays a safe no-op offline/empty.

Every download is mocked by monkeypatching ``httpx.get`` (glossary.py does a local ``import httpx``
inside load_glossary/sync_glossary, so patching the real module's ``get`` is what the function sees).
Nothing here touches the network; all archives are built in memory and all files use tmp_path.
"""

from __future__ import annotations

import io
import zipfile

import httpx
import pytest

from dqxclarity.translate.glossary import (
    CUSTOM_TRANSLATIONS_ZIP_URL,
    Glossary,
    extract_glossary_from_zip,
    load_glossary,
    parse_glossary_csv,
    save_glossary,
    sync_glossary,
)


def _make_zip(files: dict[str, bytes | str]) -> bytes:
    """Build a repo-archive-style zip from name -> bytes/str members."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


class _Resp:
    """Minimal httpx.Response stand-in: just ``.content`` and a no-op ``raise_for_status``."""

    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self) -> None:
        pass


# --------------------------------------------------------------------------------------------------
# extract_glossary_from_zip — both accepted locations + missing degrades to []
# --------------------------------------------------------------------------------------------------


def test_extract_glossary_from_csv_dir_member():
    # The glossary lives under .../csv/glossary.csv in the repo archive (update.py:46). Other csv
    # members (merge.xlsx) and json members must be ignored.
    content = _make_zip(
        {
            "dqx-custom-translations-main/csv/glossary.csv": "スライム,Slime\nドラキー,Dracky\n",
            "dqx-custom-translations-main/csv/merge.xlsx": b"not a glossary",
            "dqx-custom-translations-main/json/foo.json": b"{}",
        }
    )
    rows = extract_glossary_from_zip(content)
    assert ("スライム", "Slime") in rows
    assert ("ドラキー", "Dracky") in rows
    assert len(rows) == 2


def test_extract_glossary_from_generate_glossary_dir_member():
    # The regex also accepts the .../generate_glossary/glossary.csv location (glossary.py:40-41).
    content = _make_zip(
        {
            "dqx-custom-translations-main/generate_glossary/glossary.csv": "魔王,Demon Lord\n",
        }
    )
    assert extract_glossary_from_zip(content) == [("魔王", "Demon Lord")]


def test_extract_glossary_from_zip_no_member_is_empty():
    # A repo archive with no glossary member degrades to [] (no-op glossary), never raises. A
    # ``glossary.csv`` that is NOT under csv/ or generate_glossary/ must NOT match the regex.
    content = _make_zip(
        {
            "dqx-custom-translations-main/README.md": b"hi",
            "dqx-custom-translations-main/json/glossary.csv": "見えない,Hidden\n",  # wrong dir
        }
    )
    assert extract_glossary_from_zip(content) == []


# --------------------------------------------------------------------------------------------------
# save_glossary + parse_glossary_csv round-trip
# --------------------------------------------------------------------------------------------------


def test_save_glossary_accepts_mapping(tmp_path):
    # save_glossary takes either an iterable of pairs OR a Mapping (glossary.py:198-200). A dict
    # round-trips through parse_glossary_csv to the same (ja, en) rows.
    path = save_glossary({"スライム": "Slime", "ドラキー": "Dracky"}, tmp_path)
    assert path == tmp_path / "glossary.csv"
    loaded = dict(parse_glossary_csv(path.read_bytes()))
    assert loaded == {"スライム": "Slime", "ドラキー": "Dracky"}


def test_parse_glossary_csv_first_comma_split_roundtrip(tmp_path):
    # An EN value containing a comma survives the first-comma split (update.py:216-217). save_glossary
    # uses csv.writer which RFC-quotes that field; the documented limitation is that the quotes are
    # NOT stripped on read-back, so we feed parse_glossary_csv a raw unquoted line to prove the
    # split itself keeps the EN comma.
    rows = parse_glossary_csv("名前,Name, with comma\nスライム,Slime\n")
    assert ("名前", "Name, with comma") in rows
    assert ("スライム", "Slime") in rows
    assert len(rows) == 2


def test_parse_glossary_csv_skips_blank_and_commaless_and_empty_sides():
    # Blank lines, lines with no comma, and rows whose JA or EN strips to empty are all dropped
    # (glossary.py:119-126). \r\n line endings are tolerated (record.rstrip('\r')).
    text = "アスト,Ast\r\n\nbroken_line\n,OnlyEn\n空,\n  \n王,King\n"
    rows = parse_glossary_csv(text)
    assert ("アスト", "Ast") in rows
    assert ("王", "King") in rows
    # ",OnlyEn" has empty JA; "空," has empty EN; both dropped along with blank/comma-less lines.
    assert len(rows) == 2


# --------------------------------------------------------------------------------------------------
# load_glossary — three-step resolution order (snapshot -> download -> empty)
# --------------------------------------------------------------------------------------------------


def test_load_glossary_prefers_local_snapshot_no_download(tmp_path, monkeypatch):
    # A saved snapshot is loaded directly and NO download is attempted — patch httpx.get to blow up
    # if it is called, proving step 1 short-circuits steps 2/3.
    save_glossary([("スライム", "Slime")], tmp_path)

    def _boom(*a, **k):
        raise AssertionError("load_glossary must not download when a snapshot exists")

    monkeypatch.setattr(httpx, "get", _boom)
    g = load_glossary(tmp_path)
    assert len(g) == 1
    assert g.glossify("スライムが現れた") == "Slime が現れた"


def test_load_glossary_downloads_and_persists_when_no_snapshot(tmp_path, monkeypatch):
    # No snapshot yet: load_glossary fetches the repo zip (mocked), extracts glossary.csv, builds the
    # Glossary, AND persists a snapshot next to the cache so the next run skips the fetch.
    zip_bytes = _make_zip(
        {"dqx-custom-translations-main/csv/glossary.csv": "魔王,Demon Lord\n勇者,Hero\n"}
    )
    seen: list[str] = []

    def fake_get(url, **kwargs):
        seen.append(url)
        return _Resp(zip_bytes)

    monkeypatch.setattr(httpx, "get", fake_get)
    g = load_glossary(tmp_path)

    assert seen == [CUSTOM_TRANSLATIONS_ZIP_URL]  # exactly one fetch of the whole-repo archive
    assert len(g) == 2
    assert g.glossify("魔王と勇者") == "Demon Lord と Hero "
    # The snapshot was persisted: a second load reads it back WITHOUT downloading.
    assert (tmp_path / "glossary.csv").exists()

    def _boom(*a, **k):
        raise AssertionError("second load must use the persisted snapshot, not re-download")

    monkeypatch.setattr(httpx, "get", _boom)
    g2 = load_glossary(tmp_path)
    assert len(g2) == 2


def test_load_glossary_offline_returns_empty_noop(tmp_path, monkeypatch):
    # On any download failure (offline / network error) load_glossary swallows it and returns an
    # EMPTY glossary — glossify is then a safe no-op so translation proceeds untouched.
    def fake_get(url, **kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "get", fake_get)
    g = load_glossary(tmp_path)
    assert not g
    assert g.glossify("変わらない") == "変わらない"
    # A failed download must NOT have left a snapshot behind.
    assert not (tmp_path / "glossary.csv").exists()


def test_load_glossary_download_disabled_returns_empty(tmp_path, monkeypatch):
    # download=False with no snapshot -> empty no-op glossary, and httpx.get is never touched.
    def _boom(*a, **k):
        raise AssertionError("download=False must not fetch")

    monkeypatch.setattr(httpx, "get", _boom)
    g = load_glossary(tmp_path, download=False)
    assert not g
    assert g.glossify("そのまま") == "そのまま"


def test_load_glossary_no_cache_dir_download_disabled():
    # No cache_dir AND download disabled is the pure-offline construction path: empty no-op glossary.
    g = load_glossary(None, download=False)
    assert not g
    assert len(g) == 0


# --------------------------------------------------------------------------------------------------
# sync_glossary — download (mocked) + persist + return term count
# --------------------------------------------------------------------------------------------------


def test_sync_glossary_writes_snapshot_and_returns_count(tmp_path, monkeypatch):
    zip_bytes = _make_zip(
        {"dqx-custom-translations-main/csv/glossary.csv": "スライム,Slime\nドラキー,Dracky\nメタル,Metal\n"}
    )
    seen: list[str] = []

    def fake_get(url, **kwargs):
        seen.append(url)
        return _Resp(zip_bytes)

    monkeypatch.setattr(httpx, "get", fake_get)
    count = sync_glossary(tmp_path)

    assert count == 3
    assert seen == [CUSTOM_TRANSLATIONS_ZIP_URL]
    # The snapshot landed on disk and parses back to the same three terms.
    snapshot = tmp_path / "glossary.csv"
    assert snapshot.exists()
    assert dict(parse_glossary_csv(snapshot.read_bytes())) == {
        "スライム": "Slime",
        "ドラキー": "Dracky",
        "メタル": "Metal",
    }


def test_sync_glossary_raises_on_download_failure(tmp_path, monkeypatch):
    # Unlike load_glossary, sync_glossary RAISES on failure (the CLI catches/reports it) — it must
    # NOT swallow the error into an empty glossary.
    def fake_get(url, **kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "get", fake_get)
    with pytest.raises(httpx.ConnectError):
        sync_glossary(tmp_path)
    assert not (tmp_path / "glossary.csv").exists()


def test_sync_glossary_empty_archive_writes_empty_snapshot(tmp_path, monkeypatch):
    # A repo archive with no glossary member yields 0 terms; sync still writes an (empty) snapshot
    # and returns 0 — a degenerate-but-non-raising success.
    zip_bytes = _make_zip({"dqx-custom-translations-main/README.md": b"hi"})
    monkeypatch.setattr(httpx, "get", lambda url, **k: _Resp(zip_bytes))
    assert sync_glossary(tmp_path) == 0
    assert (tmp_path / "glossary.csv").exists()
    assert parse_glossary_csv((tmp_path / "glossary.csv").read_bytes()) == []


# --------------------------------------------------------------------------------------------------
# compiled matcher — many keys + meta chars, longest-first in one pass; offline/empty no-op
# --------------------------------------------------------------------------------------------------


def test_compiled_matcher_many_keys_longest_first_and_meta_chars():
    # A glossary built from many keys (including ones that are prefixes of others, and keys with
    # regex-meta chars) substitutes longest-first in a single regex pass: "アストルティア" wins over
    # its prefix "アスト", and "(特技)" / "ホイミ*" match literally.
    g = Glossary(
        [
            ("アスト", "Ast"),
            ("アストルティア", "Astoltia"),
            ("スライム", "Slime"),
            ("(特技)", "Skill"),
            ("ホイミ*", "Heal"),
            ("魔王", "Demon Lord"),
        ]
    )
    assert len(g) == 6
    # Longest-first: the full name beats its prefix even though both are present.
    assert g.glossify("アストルティアでスライムと戦う") == "Astoltia で Slime と戦う"
    # Meta-char keys are matched literally (re.escape), not interpreted as a pattern.
    assert g.glossify("(特技)とホイミ*") == "Skill と Heal "
    # Back-to-back hits collapse the padded double space and the LEADING space is stripped; the
    # trailing pad of the final hit is preserved (upstream lstrip-only, translate.py:48-50).
    assert g.glossify("魔王アスト") == "Demon Lord Ast "


def test_glossify_empty_text_is_passthrough():
    # An empty input short-circuits to itself even on a populated glossary (glossary.py:88).
    g = Glossary([("スライム", "Slime")])
    assert g.glossify("") == ""


def test_offline_empty_glossary_glossify_is_safe_noop():
    # The offline path produces an empty Glossary; glossify returns its input unchanged for arbitrary
    # text, so a failed download never corrupts the MT hot path.
    g = Glossary([])
    assert not g
    assert g.glossify("魔王アストルティアスライム") == "魔王アストルティアスライム"


def test_glossary_drops_duplicate_and_empty_ja_keys():
    # Construction preserves the FIRST occurrence of a JA key and drops duplicates / empty sides
    # (glossary.py:54-57), so a later duplicate cannot clobber the first mapping.
    g = Glossary(
        [
            ("スライム", "Slime"),
            ("スライム", "WRONG"),  # duplicate JA -> ignored (first wins)
            ("", "Empty JA"),  # empty JA -> dropped
            ("空EN", ""),  # empty EN -> dropped
        ]
    )
    assert len(g) == 1
    assert g.glossify("スライム") == "Slime "
