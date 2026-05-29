"""File-patching engine: download → verify → back up → atomic replace, plus restore.

Safety model:
  * **Never patch while the game is running** — files are mmap'd; replacing them mid-session
    risks corruption. Hard block unless ``force``.
  * Verify sha256/size before installing *when the manifest provides them* (upstream doesn't).
  * **Idempotent**: each asset is downloaded to a cache, then compared (by sha256) against the
    installed file; identical files are skipped, so re-running ``patch`` is a no-op.
  * Back up every original to a timestamped backup set before overwriting. Files that did not
    exist before (e.g. the added ``data00000000.win32.dat1``) are recorded as "added" so
    ``restore`` removes them rather than trying to roll them back.
  * Replace atomically via a temp file in the same directory + ``os.replace``.

No admin/root needed on Linux: the install lives under the user's home and is user-writable
(the Windows ``IsUserAnAdmin`` check upstream uses is a Windows ACL concern that doesn't apply).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..process.discover import GameInstall, find_game_pid
from .manifest import Manifest, PatchFile, PatchGroup


@dataclass
class PlannedFile:
    pf: PatchFile
    group: str


def sha256_file(path: Path, _chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_chunk):
            h.update(chunk)
    return h.hexdigest()


def is_game_running() -> bool:
    return find_game_pid() is not None


def _select(install: GameInstall, groups: list[PatchGroup]) -> list[PlannedFile]:
    planned: list[PlannedFile] = []
    missing: list[str] = []
    for g in groups:
        for pf in g.files:
            if not (install.install_root / pf.target).parent.is_dir():
                missing.append(pf.target)
            planned.append(PlannedFile(pf, g.name))
    if missing:
        raise RuntimeError(
            f"target directory missing for: {', '.join(missing)}. "
            f"Is the install root correct ({install.install_root})?"
        )
    return planned


def _download(url: str, dest: Path, expected_sha: str, expected_size: int) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with httpx.stream("GET", url, timeout=None, follow_redirects=True) as resp:
        resp.raise_for_status()
        with tmp.open("wb") as f:
            for chunk in resp.iter_bytes(1 << 20):
                f.write(chunk)
    if expected_size and tmp.stat().st_size != expected_size:
        got = tmp.stat().st_size
        tmp.unlink(missing_ok=True)
        raise ValueError(f"size mismatch for {url}: got {got}, want {expected_size}")
    if expected_sha and sha256_file(tmp) != expected_sha.lower():
        tmp.unlink(missing_ok=True)
        raise ValueError(f"sha256 mismatch for {url}")
    tmp.replace(dest)


def _atomic_install(src: Path, target: Path) -> None:
    tmp = target.with_suffix(target.suffix + ".new")
    shutil.copy2(src, tmp)
    os.replace(tmp, target)  # atomic within the same filesystem


def apply(
    install: GameInstall,
    manifest: Manifest,
    *,
    requested_groups: set[str] | None,
    cache_dir: Path,
    backup_dir: Path,
    force: bool = False,
    dry_run: bool = False,
) -> dict:
    """Apply selected manifest groups. Returns a summary dict; raises on a safety block."""
    groups = manifest.resolve_groups(requested_groups)
    planned = _select(install, groups)
    summary: dict = {
        "manifest": f"{manifest.name} {manifest.version}",
        "groups": [g.name for g in groups],
        "installed": [],
        "skipped_current": [],
        "dry_run": dry_run,
        "backup_set": None,
    }
    if not planned:
        return summary

    if dry_run:
        # Report only GENUINELY-stale files, mirroring the force=False real-apply skip logic
        # (line below: installed sha256 == staged sha256 -> skip). We must not download here
        # (a dry-run touches nothing and never creates the cache), so we compare the installed
        # file against the cached asset left by a prior real apply. A file is "would install"
        # only when we cannot prove it is already current:
        #   * no cached asset to compare against (never applied / cache cleared), OR
        #   * the target is missing, OR
        #   * installed sha256 != cached sha256.
        # When the cached asset matches the installed file, the file is up to date and is
        # omitted — this stops `run`'s staleness probe from crying wolf on every launch.
        would: list[str] = []
        for p in planned:
            target = install.install_root / p.pf.target
            cached = cache_dir / Path(p.pf.target).name
            if target.is_file() and cached.is_file() and sha256_file(target) == sha256_file(cached):
                continue
            would.append(p.pf.target)
        summary["would_install"] = would
        return summary

    if is_game_running() and not force:
        raise RuntimeError(
            "DQX is running. Patching live game files can corrupt your install. "
            "Close the game first (or use --force if you really mean it)."
        )

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_set = backup_dir / f"{manifest.name.replace(' ', '_')}-{stamp}"
    index: list[dict] = []
    cache_dir.mkdir(parents=True, exist_ok=True)

    for p in planned:
        pf = p.pf
        target = install.install_root / pf.target
        staged = cache_dir / Path(pf.target).name
        _download(pf.url, staged, pf.sha256, pf.size)

        # Idempotency: skip if the installed file already matches what we downloaded.
        if target.is_file() and sha256_file(target) == sha256_file(staged):
            summary["skipped_current"].append(pf.target)
            continue

        backup_set.mkdir(parents=True, exist_ok=True)
        if target.is_file():
            backup_path = backup_set / pf.target
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup_path)
            index.append({"target": pf.target, "backup": str(backup_path.relative_to(backup_set))})
        else:
            # File didn't exist before — record so restore can delete it.
            index.append({"target": pf.target, "added": True})

        _atomic_install(staged, target)
        summary["installed"].append(pf.target)

    if index:
        (backup_set / "backup.json").write_text(
            json.dumps(
                {"manifest": manifest.name, "version": manifest.version, "files": index}, indent=2
            ),
            encoding="utf-8",
        )
        summary["backup_set"] = str(backup_set)
        # The game rewrites a game_files file each session, so auto-reapply (correctly) re-installs
        # and creates a fresh backup set every launch. Prune the oldest so they don't grow unbounded;
        # keep only the most recent N for THIS manifest (never touch unrelated dirs).
        pruned = _prune_backup_sets(backup_dir, manifest.name, keep=10)
        if pruned:
            summary["pruned_backup_sets"] = [str(p) for p in pruned]
    return summary


def _backup_set_prefix(manifest_name: str) -> str:
    """The naming prefix `apply` uses for this manifest's backup sets (see ``backup_set`` above)."""
    return f"{manifest_name.replace(' ', '_')}-"


def _prune_backup_sets(backup_dir: Path, manifest_name: str, *, keep: int = 10) -> list[Path]:
    """Delete the oldest backup sets for ``manifest_name``, keeping the most recent ``keep``.

    Safety-bounded: only directories whose name starts with this manifest's
    ``"<name>-<stamp>"`` prefix AND that carry a ``backup.json`` are considered — unrelated dirs
    (a different manifest's sets, stray files) are never touched. The most-recent ``keep`` sets are
    retained; older ones are removed wholesale. Returns the directories that were deleted.
    """
    if keep < 0 or not backup_dir.is_dir():
        return []
    prefix = _backup_set_prefix(manifest_name)
    sets = [
        d for d in backup_dir.iterdir()
        if d.is_dir() and d.name.startswith(prefix) and (d / "backup.json").is_file()
    ]
    if len(sets) <= keep:
        return []
    # Newest first by mtime; everything past the keep window is pruned.
    sets.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    pruned: list[Path] = []
    for d in sets[keep:]:
        shutil.rmtree(d, ignore_errors=True)
        pruned.append(d)
    return pruned


def latest_backup_set(backup_dir: Path) -> Path | None:
    if not backup_dir.is_dir():
        return None
    sets = [d for d in backup_dir.iterdir() if d.is_dir() and (d / "backup.json").is_file()]
    return max(sets, key=lambda d: d.stat().st_mtime, default=None)


def restore(install: GameInstall, backup_set: Path, *, force: bool = False) -> dict:
    if is_game_running() and not force:
        raise RuntimeError("DQX is running. Close it before restoring original files.")
    meta = json.loads((backup_set / "backup.json").read_text(encoding="utf-8"))
    restored: list[str] = []
    removed: list[str] = []
    for entry in meta["files"]:
        target = install.install_root / entry["target"]
        if entry.get("added"):
            if target.is_file():
                target.unlink()
            removed.append(entry["target"])
        else:
            _atomic_install(backup_set / entry["backup"], target)
            restored.append(entry["target"])
    return {"backup_set": str(backup_set), "restored": restored, "removed": removed}
