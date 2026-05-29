"""Additional coverage for the file-patching layer (network mocked, tmp dirs only).

Complements tests/test_patching.py. Everything here uses ``tmp_path`` for a fake install
and a monkeypatched ``_download`` / ``httpx.get`` so nothing touches the network or a live
game process. Style mirrors the existing patching + community tests (FakeProvider-style stubs,
``monkeypatch.setattr`` on the module under test).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from dqxclarity.patching import files as pf
from dqxclarity.patching import manifest as mani
from dqxclarity.patching.manifest import Manifest


# --------------------------------------------------------------------------- #
# Shared fixtures / helpers
# --------------------------------------------------------------------------- #
def _manifest() -> Manifest:
    """Manifest with a pre-existing target (idx → backed up) and a new one (dat1 → added),
    plus optional config_exe / launcher_exe groups for group-filtering coverage."""
    return Manifest.from_dict(
        {
            "name": "test",
            "version": "1",
            "groups": {
                "game_files": {
                    "description": "main",
                    "optional": False,
                    "files": [
                        {"target": "Game/Content/Data/data00000000.win32.idx", "url": "u://idx"},
                        {"target": "Game/Content/Data/data00000000.win32.dat1", "url": "u://dat1"},
                    ],
                },
                "config_exe": {
                    "optional": True,
                    "files": [{"target": "Game/DQXConfig.exe", "url": "u://cfg"}],
                },
                "launcher_exe": {
                    "optional": True,
                    "files": [{"target": "Boot/DQXLauncher.exe", "url": "u://launch"}],
                },
            },
        }
    )


@pytest.fixture
def install(tmp_path, monkeypatch):
    """Fake install with one pre-existing target dir tree; game reported NOT running."""
    from dqxclarity.process.discover import GameInstall

    root = tmp_path / "DRAGON QUEST X"
    (root / "Game" / "Content" / "Data").mkdir(parents=True)
    (root / "Boot").mkdir(parents=True)
    (root / "Game" / "DQXGame.exe").write_bytes(b"MZ original game exe")
    (root / "Game" / "DQXConfig.exe").write_bytes(b"MZ original config")
    (root / "Boot" / "DQXLauncher.exe").write_bytes(b"MZ original launcher")
    (root / "Game" / "Content" / "Data" / "data00000000.win32.idx").write_bytes(b"ORIGINAL-IDX")
    monkeypatch.setattr(pf, "is_game_running", lambda: False)
    return GameInstall(install_root=root)


def _fake_download(contents: dict[str, bytes]):
    """Replacement for files._download that writes canned bytes instead of fetching."""

    def _dl(url, dest, expected_sha, expected_size):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(contents[url])

    return _dl


# --------------------------------------------------------------------------- #
# apply(): install, backup-set contents, idempotency, dry_run
# --------------------------------------------------------------------------- #
def test_apply_installs_and_writes_backup_set(install, tmp_path, monkeypatch):
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
    data = install.install_root / "Game" / "Content" / "Data"
    # Listed files installed.
    assert (data / "data00000000.win32.idx").read_bytes() == b"NEW-IDX"
    assert (data / "data00000000.win32.dat1").read_bytes() == b"NEW-DAT1"
    assert set(summary["installed"]) == {
        "Game/Content/Data/data00000000.win32.idx",
        "Game/Content/Data/data00000000.win32.dat1",
    }
    assert summary["skipped_current"] == []
    assert summary["dry_run"] is False

    # A backup set was written with a parseable backup.json index.
    backup_set = Path(summary["backup_set"])
    assert backup_set.is_dir()
    meta = json.loads((backup_set / "backup.json").read_text(encoding="utf-8"))
    assert meta["manifest"] == "test"
    assert meta["version"] == "1"
    by_target = {e["target"]: e for e in meta["files"]}
    # Pre-existing idx was copied into the backup set verbatim.
    assert (backup_set / "Game/Content/Data/data00000000.win32.idx").read_bytes() == b"ORIGINAL-IDX"
    assert "backup" in by_target["Game/Content/Data/data00000000.win32.idx"]
    # New dat1 recorded as "added" (no backup copy stored for it).
    assert by_target["Game/Content/Data/data00000000.win32.dat1"].get("added") is True


def test_apply_idempotent_second_run_skips(install, tmp_path, monkeypatch):
    monkeypatch.setattr(
        pf, "_download", _fake_download({"u://idx": b"NEW-IDX", "u://dat1": b"NEW-DAT1"})
    )
    kw = dict(
        requested_groups={"game_files"},
        cache_dir=tmp_path / "cache",
        backup_dir=tmp_path / "backups",
    )
    first = pf.apply(install, _manifest(), **kw)
    assert set(first["installed"]) == {
        "Game/Content/Data/data00000000.win32.idx",
        "Game/Content/Data/data00000000.win32.dat1",
    }
    second = pf.apply(install, _manifest(), **kw)
    # Nothing re-installed; both files reported as already current.
    assert second["installed"] == []
    assert set(second["skipped_current"]) == {
        "Game/Content/Data/data00000000.win32.idx",
        "Game/Content/Data/data00000000.win32.dat1",
    }
    # No new backup set written when nothing changed.
    assert second["backup_set"] is None


def test_apply_dry_run_reports_without_writing(install, tmp_path, monkeypatch):
    # _download must NOT be called in dry-run; wire it to explode if it is.
    def _boom(*a, **k):
        raise AssertionError("download must not run during dry_run")

    monkeypatch.setattr(pf, "_download", _boom)
    data = install.install_root / "Game" / "Content" / "Data"
    before = (data / "data00000000.win32.idx").read_bytes()

    summary = pf.apply(
        install,
        _manifest(),
        requested_groups={"game_files"},
        cache_dir=tmp_path / "cache",
        backup_dir=tmp_path / "backups",
        dry_run=True,
    )
    assert summary["dry_run"] is True
    assert set(summary["would_install"]) == {
        "Game/Content/Data/data00000000.win32.idx",
        "Game/Content/Data/data00000000.win32.dat1",
    }
    assert summary["installed"] == []
    assert summary["backup_set"] is None
    # Filesystem untouched: existing file unchanged, new file not created, no backup dir.
    assert (data / "data00000000.win32.idx").read_bytes() == before
    assert not (data / "data00000000.win32.dat1").exists()
    assert not (tmp_path / "backups").exists()
    assert not (tmp_path / "cache").exists()


def test_apply_empty_groups_returns_no_op_summary(install, tmp_path, monkeypatch):
    # A manifest whose selected groups contain no files → early return, nothing downloaded.
    def _boom(*a, **k):
        raise AssertionError("download must not run when there are no planned files")

    monkeypatch.setattr(pf, "_download", _boom)
    empty = Manifest.from_dict(
        {"name": "empty", "version": "0", "groups": {"game_files": {"files": []}}}
    )
    summary = pf.apply(
        install,
        empty,
        requested_groups={"game_files"},
        cache_dir=tmp_path / "cache",
        backup_dir=tmp_path / "backups",
    )
    assert summary["installed"] == []
    assert summary["skipped_current"] == []
    assert summary["backup_set"] is None


def test_apply_raises_when_target_dir_missing(install, tmp_path, monkeypatch):
    # Manifest points at a subdirectory that doesn't exist under the install root.
    monkeypatch.setattr(pf, "_download", _fake_download({"u://x": b"X"}))
    m = Manifest.from_dict(
        {
            "name": "test",
            "version": "1",
            "groups": {
                "game_files": {
                    "files": [{"target": "No/Such/Dir/file.dat", "url": "u://x"}],
                }
            },
        }
    )
    with pytest.raises(RuntimeError, match="target directory missing"):
        pf.apply(
            install,
            m,
            requested_groups={"game_files"},
            cache_dir=tmp_path / "cache",
            backup_dir=tmp_path / "backups",
        )


# --------------------------------------------------------------------------- #
# restore() + latest_backup_set()
# --------------------------------------------------------------------------- #
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
    # Backed-up file rolled back to its original content.
    assert (data / "data00000000.win32.idx").read_bytes() == b"ORIGINAL-IDX"
    assert "Game/Content/Data/data00000000.win32.idx" in res["restored"]
    # Added file removed entirely.
    assert not (data / "data00000000.win32.dat1").exists()
    assert "Game/Content/Data/data00000000.win32.dat1" in res["removed"]
    assert res["backup_set"] == summary["backup_set"]


def test_restore_blocked_while_game_running_without_force(install, tmp_path, monkeypatch):
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
    backup_set = Path(summary["backup_set"])
    # Now pretend the game came up; restore must refuse without force.
    monkeypatch.setattr(pf, "is_game_running", lambda: True)
    with pytest.raises(RuntimeError, match="DQX is running"):
        pf.restore(install, backup_set)
    # ...but force=True lets it through.
    res = pf.restore(install, backup_set, force=True)
    assert "Game/Content/Data/data00000000.win32.idx" in res["restored"]


def test_latest_backup_set_picks_newest(tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    older = backup_dir / "test-20200101-000000"
    newer = backup_dir / "test-20300101-000000"
    for d in (older, newer):
        d.mkdir()
        (d / "backup.json").write_text("{}", encoding="utf-8")
    # Force a deterministic mtime ordering (newer is more recent).
    os.utime(older, (1_000_000_000, 1_000_000_000))
    os.utime(newer, (2_000_000_000, 2_000_000_000))
    assert pf.latest_backup_set(backup_dir) == newer


def test_latest_backup_set_ignores_dirs_without_index(tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    valid = backup_dir / "valid-set"
    valid.mkdir()
    (valid / "backup.json").write_text("{}", encoding="utf-8")
    # A newer dir lacking backup.json must be ignored.
    bogus = backup_dir / "no-index-set"
    bogus.mkdir()
    os.utime(valid, (1_000_000_000, 1_000_000_000))
    os.utime(bogus, (2_000_000_000, 2_000_000_000))
    assert pf.latest_backup_set(backup_dir) == valid


def test_latest_backup_set_none_when_missing_or_empty(tmp_path):
    # Directory does not exist at all.
    assert pf.latest_backup_set(tmp_path / "does-not-exist") is None
    # Directory exists but holds no valid backup sets.
    empty = tmp_path / "backups"
    empty.mkdir()
    assert pf.latest_backup_set(empty) is None


def test_apply_then_latest_backup_set_finds_it(install, tmp_path, monkeypatch):
    # End-to-end: after apply writes a set, latest_backup_set returns exactly that path.
    monkeypatch.setattr(
        pf, "_download", _fake_download({"u://idx": b"NEW-IDX", "u://dat1": b"NEW-DAT1"})
    )
    backup_dir = tmp_path / "backups"
    summary = pf.apply(
        install,
        _manifest(),
        requested_groups={"game_files"},
        cache_dir=tmp_path / "cache",
        backup_dir=backup_dir,
    )
    assert pf.latest_backup_set(backup_dir) == Path(summary["backup_set"])


# --------------------------------------------------------------------------- #
# Game-running guard on apply()
# --------------------------------------------------------------------------- #
def test_apply_blocked_when_pid_present(install, tmp_path, monkeypatch):
    # The `install` fixture forces is_game_running()->False; override it True to drive the guard
    # (patching find_game_pid wouldn't work — the fixture's is_game_running stub shadows it).
    monkeypatch.setattr(pf, "is_game_running", lambda: True)
    monkeypatch.setattr(
        pf, "_download", _fake_download({"u://idx": b"NEW-IDX", "u://dat1": b"NEW-DAT1"})
    )
    with pytest.raises(RuntimeError, match="DQX is running"):
        pf.apply(
            install,
            _manifest(),
            requested_groups={"game_files"},
            cache_dir=tmp_path / "cache",
            backup_dir=tmp_path / "backups",
        )


def test_apply_force_overrides_running_guard(install, tmp_path, monkeypatch):
    monkeypatch.setattr(pf, "find_game_pid", lambda: 4242)  # game "running"
    monkeypatch.setattr(
        pf, "_download", _fake_download({"u://idx": b"NEW-IDX", "u://dat1": b"NEW-DAT1"})
    )
    summary = pf.apply(
        install,
        _manifest(),
        requested_groups={"game_files"},
        cache_dir=tmp_path / "cache",
        backup_dir=tmp_path / "backups",
        force=True,
    )
    assert set(summary["installed"]) == {
        "Game/Content/Data/data00000000.win32.idx",
        "Game/Content/Data/data00000000.win32.dat1",
    }


def test_apply_dry_run_allowed_while_running(install, tmp_path, monkeypatch):
    # dry_run returns before the running guard, so it works even with a pid present.
    monkeypatch.setattr(pf, "find_game_pid", lambda: 4242)
    monkeypatch.setattr(
        pf, "_download", _fake_download({"u://idx": b"NEW-IDX", "u://dat1": b"NEW-DAT1"})
    )
    summary = pf.apply(
        install,
        _manifest(),
        requested_groups={"game_files"},
        cache_dir=tmp_path / "cache",
        backup_dir=tmp_path / "backups",
        dry_run=True,
    )
    assert summary["dry_run"] is True and summary["would_install"]


def test_is_game_running_reflects_find_game_pid(monkeypatch):
    monkeypatch.setattr(pf, "find_game_pid", lambda: None)
    assert pf.is_game_running() is False
    monkeypatch.setattr(pf, "find_game_pid", lambda: 99)
    assert pf.is_game_running() is True


# --------------------------------------------------------------------------- #
# manifest: load_manifest, has_files, group filtering
# --------------------------------------------------------------------------- #
def _manifest_json() -> str:
    return json.dumps(
        {
            "name": "dqx-en",
            "version": "v9",
            "groups": {
                "game_files": {
                    "description": "main",
                    "optional": False,
                    "files": [
                        {
                            "target": "Game/Content/Data/data00000000.win32.idx",
                            "url": "https://example/idx",
                        }
                    ],
                },
                "config_exe": {
                    "optional": True,
                    "files": [{"target": "Game/DQXConfig.exe", "url": "https://example/cfg"}],
                },
                "launcher_exe": {
                    "optional": True,
                    "files": [{"target": "Boot/DQXLauncher.exe", "url": "https://example/launch"}],
                },
            },
        }
    )


def test_load_manifest_from_local_path(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(_manifest_json(), encoding="utf-8")
    m = mani.load_manifest(str(path))
    assert m.name == "dqx-en"
    assert m.version == "v9"
    assert set(m.groups) == {"game_files", "config_exe", "launcher_exe"}
    assert m.groups["game_files"].optional is False
    assert m.groups["config_exe"].optional is True
    assert m.groups["game_files"].files[0].target == "Game/Content/Data/data00000000.win32.idx"


def test_load_manifest_bundled_default_when_source_empty():
    # source None / "" → the package's bundled default_manifest.json (no network).
    for src in (None, ""):
        m = mani.load_manifest(src)
        assert m.name == "dqx-en"
        assert {"game_files", "config_exe", "launcher_exe"} <= set(m.groups)
        assert m.has_files()


def test_load_manifest_from_url_uses_httpx(monkeypatch):
    captured = {}

    class _Resp:
        text = _manifest_json()

        def raise_for_status(self):
            captured["raised"] = True

    def fake_get(url, **kwargs):
        captured["url"] = url
        return _Resp()

    monkeypatch.setattr(mani.httpx, "get", fake_get)
    m = mani.load_manifest("https://example.com/manifest.json")
    assert captured["url"] == "https://example.com/manifest.json"
    assert captured.get("raised") is True
    assert m.name == "dqx-en"
    assert set(m.groups) == {"game_files", "config_exe", "launcher_exe"}


def test_has_files_empty_vs_populated():
    empty = Manifest.from_dict(
        {"name": "e", "version": "0", "groups": {"game_files": {"files": []}}}
    )
    assert empty.has_files() is False
    populated = _manifest()
    assert populated.has_files() is True
    # Also true if only an optional group carries files.
    only_optional = Manifest.from_dict(
        {
            "name": "o",
            "version": "0",
            "groups": {
                "game_files": {"files": []},
                "config_exe": {"optional": True, "files": [{"target": "Game/x", "url": "u://x"}]},
            },
        }
    )
    assert only_optional.has_files() is True


def test_group_filtering_selects_requested_entries():
    m = _manifest()
    # Default → game_files only.
    assert [g.name for g in m.resolve_groups(None)] == ["game_files"]
    # Each optional group selectable on its own with exactly its files.
    cfg = m.resolve_groups({"config_exe"})
    assert [g.name for g in cfg] == ["config_exe"]
    assert [f.target for f in cfg[0].files] == ["Game/DQXConfig.exe"]
    launch = m.resolve_groups({"launcher_exe"})
    assert [g.name for g in launch] == ["launcher_exe"]
    assert [f.target for f in launch[0].files] == ["Boot/DQXLauncher.exe"]
    # Combined selection returns both requested groups (and not the unrequested one).
    combo = {g.name for g in m.resolve_groups({"game_files", "launcher_exe"})}
    assert combo == {"game_files", "launcher_exe"}
    # Unknown group rejected.
    with pytest.raises(ValueError, match="unknown patch group"):
        m.resolve_groups({"does_not_exist"})


def test_select_planned_files_covers_requested_group_only(install):
    # Selecting only config_exe plans exactly that group's files against the fake install.
    m = _manifest()
    groups = m.resolve_groups({"config_exe"})
    planned = pf._select(install, groups)
    assert [(p.group, p.pf.target) for p in planned] == [("config_exe", "Game/DQXConfig.exe")]


def test_patchfile_rejects_unsafe_targets():
    # Defense-in-depth: absolute or parent-escaping targets are refused at model build time.
    with pytest.raises(ValueError, match="unsafe manifest target"):
        Manifest.from_dict(
            {"name": "x", "version": "0", "groups": {"g": {"files": [{"target": "/etc/passwd", "url": "u"}]}}}
        )
    with pytest.raises(ValueError, match="unsafe manifest target"):
        Manifest.from_dict(
            {"name": "x", "version": "0", "groups": {"g": {"files": [{"target": "../escape", "url": "u"}]}}}
        )
