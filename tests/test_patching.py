"""Tests for the file-patching engine (network mocked)."""

from __future__ import annotations

from pathlib import Path

import pytest

from dqxclarity.patching import files as pf
from dqxclarity.patching.manifest import Manifest


def _manifest() -> Manifest:
    return Manifest.from_dict(
        {
            "name": "test",
            "version": "1",
            "groups": {
                "game_files": {
                    "files": [
                        # existing file (will be backed up) + new file (will be "added")
                        {"target": "Game/Content/Data/data00000000.win32.idx", "url": "u://idx"},
                        {"target": "Game/Content/Data/data00000000.win32.dat1", "url": "u://dat1"},
                    ]
                },
                "config_exe": {
                    "optional": True,
                    "files": [{"target": "Game/DQXConfig.exe", "url": "u://cfg"}],
                },
            },
        }
    )


@pytest.fixture
def install(tmp_path, monkeypatch):
    """A fake install with one pre-existing target; game reported not running."""
    from dqxclarity.process.discover import GameInstall

    root = tmp_path / "DRAGON QUEST X"
    (root / "Game" / "Content" / "Data").mkdir(parents=True)
    (root / "Game" / "DQXGame.exe").write_bytes(b"MZ original game exe")
    (root / "Game" / "Content" / "Data" / "data00000000.win32.idx").write_bytes(b"ORIGINAL-IDX")
    monkeypatch.setattr(pf, "is_game_running", lambda: False)
    return GameInstall(install_root=root)


def _fake_download(contents: dict[str, bytes]):
    def _dl(url, dest, expected_sha, expected_size):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(contents[url])

    return _dl


def test_resolve_groups():
    m = _manifest()
    assert [g.name for g in m.resolve_groups(None)] == ["game_files"]  # default only
    assert {g.name for g in m.resolve_groups({"game_files", "config_exe"})} == {
        "game_files",
        "config_exe",
    }
    with pytest.raises(ValueError):
        m.resolve_groups({"nope"})


def test_apply_backs_up_existing_and_marks_added(install, tmp_path, monkeypatch):
    monkeypatch.setattr(
        pf,
        "_download",
        _fake_download({"u://idx": b"NEW-IDX", "u://dat1": b"NEW-DAT1"}),
    )
    summary = pf.apply(
        install,
        _manifest(),
        requested_groups={"game_files"},
        cache_dir=tmp_path / "cache",
        backup_dir=tmp_path / "backups",
    )
    data = install.install_root / "Game" / "Content" / "Data"
    assert (data / "data00000000.win32.idx").read_bytes() == b"NEW-IDX"
    assert (data / "data00000000.win32.dat1").read_bytes() == b"NEW-DAT1"
    assert set(summary["installed"]) == {
        "Game/Content/Data/data00000000.win32.idx",
        "Game/Content/Data/data00000000.win32.dat1",
    }
    # The pre-existing idx was backed up; the new dat1 was recorded as "added".
    backup_set = Path(summary["backup_set"])
    assert (backup_set / "Game/Content/Data/data00000000.win32.idx").read_bytes() == b"ORIGINAL-IDX"


def test_apply_is_idempotent(install, tmp_path, monkeypatch):
    monkeypatch.setattr(
        pf, "_download", _fake_download({"u://idx": b"NEW-IDX", "u://dat1": b"NEW-DAT1"})
    )
    kw = dict(
        requested_groups={"game_files"},
        cache_dir=tmp_path / "cache",
        backup_dir=tmp_path / "backups",
    )
    pf.apply(install, _manifest(), **kw)
    second = pf.apply(install, _manifest(), **kw)
    assert second["installed"] == []
    assert set(second["skipped_current"]) == {
        "Game/Content/Data/data00000000.win32.idx",
        "Game/Content/Data/data00000000.win32.dat1",
    }


def test_restore_rolls_back_and_removes_added(install, tmp_path, monkeypatch):
    monkeypatch.setattr(
        pf, "_download", _fake_download({"u://idx": b"NEW-IDX", "u://dat1": b"NEW-DAT1"})
    )
    summary = pf.apply(
        install,
        _manifest(),
        requested_groups={"game_files"},
        cache_dir=tmp_path / "cache",
        backup_dir=tmp_path / "backups",
    )
    res = pf.restore(install, Path(summary["backup_set"]))
    data = install.install_root / "Game" / "Content" / "Data"
    assert (data / "data00000000.win32.idx").read_bytes() == b"ORIGINAL-IDX"  # rolled back
    assert not (data / "data00000000.win32.dat1").exists()  # added file removed
    assert "Game/Content/Data/data00000000.win32.dat1" in res["removed"]


def test_game_running_blocks_apply(install, tmp_path, monkeypatch):
    monkeypatch.setattr(pf, "is_game_running", lambda: True)
    monkeypatch.setattr(pf, "_download", _fake_download({"u://idx": b"X", "u://dat1": b"Y"}))
    with pytest.raises(RuntimeError, match="DQX is running"):
        pf.apply(
            install,
            _manifest(),
            requested_groups={"game_files"},
            cache_dir=tmp_path / "cache",
            backup_dir=tmp_path / "backups",
        )
    # dry-run is allowed even while running (touches nothing)
    s = pf.apply(
        install,
        _manifest(),
        requested_groups={"game_files"},
        cache_dir=tmp_path / "cache",
        backup_dir=tmp_path / "backups",
        dry_run=True,
    )
    assert s["dry_run"] and s["would_install"]
