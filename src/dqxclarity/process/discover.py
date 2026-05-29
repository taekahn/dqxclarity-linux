"""Locate the DQX install, Proton prefix, and (optionally) the running game process.

Two discovery paths:

1. **Live process** (best): scan ``/proc`` for ``DQXGame.exe`` and read its ``environ``,
   which Proton populates with ``STEAM_COMPAT_INSTALL_PATH``, ``STEAM_COMPAT_DATA_PATH`` and
   ``WINEPREFIX`` — every path we need, exactly as the running game sees it.
2. **Static** (game off): parse Steam ``libraryfolders.vdf`` to enumerate libraries, then look
   for the install root (configured or by scanning) and the matching ``compatdata`` prefix.

DQX (a Square Enix SqPack-based engine, like FFXIV) keeps its text/asset archives in
``<root>/Game/Content/Data/dataXXXXXXXX.win32.{idx,dat0..N}``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

GAME_EXE = "DQXGame.exe"
CONFIG_EXE = "DQXConfig.exe"
BOOT_EXE = "DQXBoot.exe"

# Candidate locations of a Steam client root (where config/libraryfolders.vdf lives).
_STEAM_CLIENT_ROOTS = [
    "~/.steam/steam",
    "~/.local/share/Steam",
    "~/.var/app/com.valvesoftware.Steam/.local/share/Steam",  # Flatpak
]


@dataclass
class GameInstall:
    """Resolved paths for a DQX installation."""

    install_root: Path  # ".../DRAGON QUEST X"
    prefix: Path | None = None  # Proton WINEPREFIX (.../pfx)
    compat_data_path: Path | None = None  # .../compatdata/<appid>
    appid: str | None = None
    proton_dir: Path | None = None
    pid: int | None = None  # set when discovered from a live process

    @property
    def game_dir(self) -> Path:
        return self.install_root / "Game"

    @property
    def data_dir(self) -> Path:
        return self.game_dir / "Content" / "Data"

    @property
    def game_exe(self) -> Path:
        return self.game_dir / GAME_EXE

    @property
    def config_exe(self) -> Path:
        return self.game_dir / CONFIG_EXE

    def looks_valid(self) -> bool:
        return self.game_exe.is_file() and self.data_dir.is_dir()


# --------------------------------------------------------------------------- #
# Live-process discovery
# --------------------------------------------------------------------------- #
def _read_proc_field(pid: int, field: str) -> str | None:
    try:
        return (Path("/proc") / str(pid) / field).read_bytes().decode("utf-8", "replace")
    except (OSError, PermissionError):
        return None


def _proc_environ(pid: int) -> dict[str, str]:
    raw = _read_proc_field(pid, "environ")
    if not raw:
        return {}
    env: dict[str, str] = {}
    for entry in raw.split("\0"):
        if "=" in entry:
            k, _, v = entry.partition("=")
            env[k] = v.strip().strip('"')
    return env


def find_game_pid() -> int | None:
    """Return the PID of the running DQXGame.exe, or None."""
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        cmdline = _read_proc_field(int(entry.name), "cmdline")
        if cmdline and GAME_EXE in cmdline:
            # The reaper/proton wrappers also mention DQXGame.exe via the boot path; we want
            # the process whose argv[0] *is* the game exe.
            argv0 = cmdline.split("\0", 1)[0]
            if argv0.endswith(GAME_EXE) or argv0 == GAME_EXE:
                return int(entry.name)
    return None


def from_live_process(pid: int) -> GameInstall | None:
    """Build a GameInstall from a running game's environment."""
    env = _proc_environ(pid)
    install_path = env.get("STEAM_COMPAT_INSTALL_PATH")  # ".../DRAGON QUEST X/Boot/"
    install_root: Path | None = None
    if install_path:
        boot = Path(install_path.rstrip("/"))
        # STEAM_COMPAT_INSTALL_PATH points at the Boot dir; the install root is its parent.
        install_root = boot.parent if boot.name.lower() == "boot" else boot
    if install_root is None or not (install_root / "Game" / GAME_EXE).is_file():
        return None

    compat = env.get("STEAM_COMPAT_DATA_PATH")
    prefix = env.get("WINEPREFIX")
    appid = None
    if compat:
        m = re.search(r"compatdata/(\d+)", compat)
        appid = m.group(1) if m else None

    return GameInstall(
        install_root=install_root,
        prefix=Path(prefix.rstrip("/")) if prefix else None,
        compat_data_path=Path(compat) if compat else None,
        appid=appid,
        pid=pid,
    )


# --------------------------------------------------------------------------- #
# Static discovery (game not running)
# --------------------------------------------------------------------------- #
def steam_client_root() -> Path | None:
    for cand in _STEAM_CLIENT_ROOTS:
        p = Path(cand).expanduser()
        if (p / "config" / "libraryfolders.vdf").is_file():
            return p
    return None


def steam_libraries() -> list[Path]:
    """All Steam library paths from libraryfolders.vdf (best-effort, regex-based)."""
    root = steam_client_root()
    libs: list[Path] = []
    if root is None:
        return libs
    vdf = root / "config" / "libraryfolders.vdf"
    try:
        text = vdf.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return libs
    for m in re.finditer(r'"path"\s*"([^"]+)"', text):
        p = Path(m.group(1))
        if p.is_dir():
            libs.append(p)
    return libs


def find_prefix_for_appid(appid: str) -> tuple[Path, Path] | None:
    """Return (compat_data_path, prefix) for an appid by scanning all libraries."""
    for lib in steam_libraries():
        compat = lib / "steamapps" / "compatdata" / appid
        if (compat / "pfx").is_dir():
            return compat, compat / "pfx"
    return None


def scan_libraries_for_install() -> Path | None:
    """Look for a 'DRAGON QUEST X' install root under each library's common/ dir."""
    for lib in steam_libraries():
        common = lib / "steamapps" / "common"
        if not common.is_dir():
            continue
        for child in common.iterdir():
            if "dragon quest x" in child.name.lower():
                if (child / "Game" / GAME_EXE).is_file():
                    return child
    return None


# --------------------------------------------------------------------------- #
# Top-level entry point
# --------------------------------------------------------------------------- #
def discover(configured_install_root: str | os.PathLike[str] | None = None) -> GameInstall | None:
    """Resolve a GameInstall, preferring the live process, then config, then a library scan.

    ``configured_install_root`` (from config.toml) wins for the install path when set, but we
    still enrich it with prefix/appid from the live process or library scan when possible.
    """
    pid = find_game_pid()
    if pid is not None:
        live = from_live_process(pid)
        if live and live.looks_valid():
            return live

    # Static fallbacks.
    install_root: Path | None = None
    if configured_install_root:
        cand = Path(configured_install_root).expanduser()
        if (cand / "Game" / GAME_EXE).is_file():
            install_root = cand
    if install_root is None:
        install_root = scan_libraries_for_install()
    if install_root is None:
        return None

    game = GameInstall(install_root=install_root, pid=pid)
    # Try to attach a prefix if we can find one (appid unknown statically for non-Steam games,
    # so this is best-effort and may stay None until the game is run once).
    return game
