"""Preflight environment checks for the CLI ``doctor`` command."""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from . import config as cfg_mod
from .process.discover import GameInstall, discover, find_game_pid


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def _ptrace_scope() -> str | None:
    p = Path("/proc/sys/kernel/yama/ptrace_scope")
    try:
        return p.read_text().strip()
    except OSError:
        return None


def run_checks() -> tuple[list[Check], GameInstall | None]:
    cfg = cfg_mod.load()
    checks: list[Check] = []

    checks.append(
        Check("python", sys.version_info >= (3, 11), f"Python {sys.version.split()[0]}")
    )

    install = discover(cfg.install_root or None)
    if install and install.looks_valid():
        checks.append(Check("install", True, str(install.install_root)))
        checks.append(
            Check("data dir", install.data_dir.is_dir(), str(install.data_dir))
        )
        checks.append(
            Check(
                "prefix",
                True,  # only needed for live translation (Phase 3), not file patching
                str(install.prefix)
                if install.prefix
                else "unknown — only needed for live translation; run the game once to detect",
            )
        )
        free = shutil.disk_usage(install.install_root).free // (1024 * 1024)
        checks.append(Check("free space", free > 2048, f"{free} MiB free in install volume"))
    else:
        checks.append(
            Check(
                "install",
                False,
                "DQX install not found. Run the game once (auto-detect), or set "
                "install_root in config.",
            )
        )

    pid = find_game_pid()
    checks.append(
        Check(
            "game running",
            True,  # informational, not a failure either way
            f"yes (pid {pid}) — close it before patching" if pid else "no (ok for patching)",
        )
    )

    # Live-translation prerequisites (informational for Phase 1).
    scope = _ptrace_scope()
    checks.append(
        Check(
            "ptrace_scope",
            True,
            f"{scope} (same-uid memory access works regardless on this kernel)"
            if scope is not None
            else "unknown",
        )
    )
    # Hooking backend: native ptrace detours (Frida was evaluated and rejected — it cannot attach
    # to the Proton WOW64 process). See PLAN.md §5b. process_vm_readv/writev is the mechanism.
    checks.append(
        Check(
            "hook backend",
            True,
            "native detours (process_vm_readv/writev) — Frida not used (WOW64 attach unsupported)",
        )
    )

    return checks, install
