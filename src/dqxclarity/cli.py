"""dqxclarity-linux command-line interface."""

from __future__ import annotations

from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from . import config as cfg_mod
from . import doctor as doctor_mod
from .patching import files as patch_files
from .patching.manifest import load_manifest
from .process.discover import discover, find_game_pid, from_live_process
from .runtime import hookjournal

app = typer.Typer(
    add_completion=False,
    help="Linux-native CLI fork of dqxclarity — translate Dragon Quest X under Steam/Proton.",
    no_args_is_help=True,
)
console = Console()


def _resolve_install(cfg: cfg_mod.Config):
    """Discover the install and, if it came from the live process, remember its path.

    Non-Steam-game installs often live outside a Steam library, so static discovery can't find
    them when the game is closed. Caching install_root after a live discovery means the user
    only needs the game running once.
    """
    install = discover(cfg.install_root or None)
    # When the game is running we trust its live path; refresh config if it's new or moved
    # (the user may reinstall to a different location).
    if install and install.pid is not None and cfg.install_root != str(install.install_root):
        cfg.install_root = str(install.install_root)
        cfg_mod.save(cfg)
    return install


def _patch_groups(cfg: cfg_mod.Config, *, config_exe: bool = False, launcher_exe: bool = False) -> set[str]:
    """The patch groups to act on, honouring config toggles plus any explicit overrides."""
    groups = {"game_files"}
    if config_exe or cfg.patch.patch_config_exe:
        groups.add("config_exe")
    if launcher_exe or cfg.patch.patch_launcher_exe:
        groups.add("launcher_exe")
    return groups


def _apply_patches_for_run(cfg: cfg_mod.Config) -> None:
    """Reapply static file patches on `run` startup, when it's safe to do so.

    Mirrors the `patch` command's manifest/install/cache/backup setup (kept in one place so the
    two paths can't drift). The safety contract:

      * Game NOT running -> apply for real (force=False, dry_run=False) and print a concise
        summary. patch_files.apply is idempotent, so an up-to-date install is a cheap no-op.
      * Game ALREADY running -> never patch live mmap'd files. Run a dry-run staleness probe and,
        if anything WOULD install, warn the user to close the game and re-run. Any probe error is
        swallowed so a flaky staleness check never blocks attaching.

    Best-effort throughout: a missing install or manifest just prints a note and returns; it must
    never abort `run`, which can still attach to an already-launched game.
    """
    install = _resolve_install(cfg)
    if not install or not install.looks_valid():
        console.print("[dim]skipping patch step: no DQX install located yet.[/]")
        return
    try:
        manifest = load_manifest(cfg.patch.manifest_url or None)
    except (httpx.HTTPError, ValueError, OSError) as e:
        console.print(f"[yellow]skipping patch step: could not load manifest ({e}).[/]")
        return
    if not manifest.has_files():
        console.print("[dim]skipping patch step: manifest lists no files to patch.[/]")
        return

    groups = _patch_groups(cfg)
    cache_dir = cfg_mod.CONFIG_DIR / "cache"
    backup_dir = Path(cfg.backup_dir)

    if patch_files.is_game_running():
        # Can't safely overwrite a running game's files; just probe for staleness and warn.
        try:
            probe = patch_files.apply(
                install, manifest, requested_groups=groups,
                cache_dir=cache_dir, backup_dir=backup_dir, force=False, dry_run=True,
            )
        except Exception:  # noqa: BLE001 - a staleness probe must never block attaching
            return
        # The probe now compares each installed file against its cached asset (see
        # patch_files.apply dry_run), so this list is the genuinely-stale (or never-cached,
        # hence unverifiable) files — no longer a blanket "all files" false alarm.
        stale = probe.get("would_install", [])
        if stale:
            console.print(
                f"[yellow]{len(stale)} patch file(s) may need updating — close the game and "
                f"re-run, or verify with `dqxclarity patch --dry-run`.[/]"
            )
        return

    # Game is down: safe to apply for real.
    try:
        summary = patch_files.apply(
            install, manifest, requested_groups=groups,
            cache_dir=cache_dir, backup_dir=backup_dir, force=False, dry_run=False,
        )
    except (RuntimeError, ValueError, httpx.HTTPError, OSError) as e:
        # OSError covers a read-only game dir / full disk during _download or _atomic_install
        # (mkdir, copy2, os.replace). Patching is best-effort; never abort run on a filesystem
        # hiccup — print a note and continue to attach.
        console.print(f"[yellow]patch step skipped: {e}[/]")
        return
    n_installed = len(summary.get("installed", []))
    n_current = len(summary.get("skipped_current", []))
    console.print(f"patches: [green]{n_installed} installed[/], {n_current} already up to date.")


def _wait_for_game(poll: float = 1.5) -> int:
    """Poll until DQXGame.exe appears, returning its pid. Ctrl-C exits cleanly (no traceback)."""
    import time

    announced = False
    try:
        while True:
            pid = find_game_pid()
            if pid is not None:
                return pid
            if not announced:
                console.print("Waiting for DQXGame.exe to start… (Ctrl-C to cancel)")
                announced = True
            time.sleep(poll)
    except KeyboardInterrupt:
        console.print("[yellow]cancelled.[/]")
        raise typer.Exit(code=0)


def _version_cb(value: bool) -> None:
    if value:
        console.print(f"dqxclarity-linux {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    _v: bool = typer.Option(
        False, "--version", callback=_version_cb, is_eager=True, help="Show version and exit."
    ),
) -> None:
    pass


@app.command()
def doctor() -> None:
    """Run preflight checks (install detection, paths, permissions, deps)."""
    checks, _install = doctor_mod.run_checks()
    table = Table(title="dqxclarity doctor")
    table.add_column("check")
    table.add_column("")
    table.add_column("detail", overflow="fold")
    for c in checks:
        mark = "[green]OK[/]" if c.ok else "[red]FAIL[/]"
        table.add_row(c.name, mark, c.detail)
    console.print(table)
    if any(not c.ok for c in checks):
        raise typer.Exit(code=1)


@app.command()
def patch(
    config_exe: bool = typer.Option(False, "--config", help="Also patch DQXConfig.exe."),
    launcher_exe: bool = typer.Option(False, "--launcher", help="Also patch DQXLauncher.exe."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would change, touch nothing."),
    force: bool = typer.Option(False, "--force", help="Patch even if the game is running (risky)."),
) -> None:
    """Apply translated game files (static file patching).

    By default patches the translated UI/menu archive (game_files). Add --config / --launcher
    (or set patch.patch_config_exe / patch.patch_launcher_exe in config) to also patch the
    config tool and boot launcher executables.
    """
    cfg = cfg_mod.load()
    install = _resolve_install(cfg)
    if not install or not install.looks_valid():
        console.print("[red]Could not locate a DQX install.[/] Run the game once, or set "
                      "install_root via `dqxclarity config set install_root <path>`.")
        raise typer.Exit(code=1)

    manifest = load_manifest(cfg.patch.manifest_url or None)
    if not manifest.has_files():
        console.print(f"[yellow]Manifest '{manifest.name}' lists no files to patch.[/]")
        raise typer.Exit(code=1)

    groups = _patch_groups(cfg, config_exe=config_exe, launcher_exe=launcher_exe)

    try:
        summary = patch_files.apply(
            install,
            manifest,
            requested_groups=groups,
            cache_dir=cfg_mod.CONFIG_DIR / "cache",
            backup_dir=Path(cfg.backup_dir),
            force=force,
            dry_run=dry_run,
        )
    except (RuntimeError, ValueError, httpx.HTTPError) as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(code=1)

    console.print(f"manifest: [bold]{summary['manifest']}[/]  groups: {', '.join(summary['groups'])}")
    if summary.get("dry_run"):
        for t in summary.get("would_install", []):
            console.print(f"  would install: {t}")
        console.print("[dim](apply downloads each asset and skips files already up to date)[/]")
    else:
        for t in summary["installed"]:
            console.print(f"  [green]installed[/]: {t}")
        for t in summary["skipped_current"]:
            console.print(f"  [dim]up to date: {t}[/]")
        if summary["backup_set"]:
            console.print(f"backup saved to: {summary['backup_set']}")
        elif not summary["installed"]:
            console.print("[dim]nothing to do — already up to date.[/]")


@app.command()
def restore(
    force: bool = typer.Option(False, "--force", help="Restore even if the game is running."),
) -> None:
    """Restore original game files from the most recent backup."""
    cfg = cfg_mod.load()
    install = _resolve_install(cfg)
    if not install or not install.looks_valid():
        console.print("[red]Could not locate a DQX install.[/]")
        raise typer.Exit(code=1)
    backup_set = patch_files.latest_backup_set(Path(cfg.backup_dir))
    if backup_set is None:
        console.print("[yellow]No backups found.[/]")
        raise typer.Exit(code=1)
    try:
        summary = patch_files.restore(install, backup_set, force=force)
    except (RuntimeError, ValueError) as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(code=1)
    for t in summary["restored"]:
        console.print(f"  [green]restored[/]: {t}")
    for t in summary["removed"]:
        console.print(f"  [green]removed[/] (was added by patch): {t}")
    if not summary["restored"] and not summary["removed"]:
        console.print("[dim]backup contained no files to restore.[/]")


config_app = typer.Typer(help="View and edit configuration.", no_args_is_help=True)
app.add_typer(config_app, name="config")


@config_app.command("show")
def config_show() -> None:
    """Print the current configuration and its file path."""
    cfg = cfg_mod.load()
    console.print(f"config file: {cfg_mod.CONFIG_FILE}")
    console.print(cfg)


@config_app.command("set")
def config_set(key: str, value: str) -> None:
    """Set a config value. Keys: install_root, backup_dir, patch.manifest_url,
    translate.provider, translate.api_key."""
    import dataclasses

    def _is_field(obj: object, name: str) -> bool:
        return dataclasses.is_dataclass(obj) and any(f.name == name for f in dataclasses.fields(obj))

    cfg = cfg_mod.load()
    table, _, leaf = key.partition(".")
    # Validate against real dataclass fields. A slot-less dataclass would otherwise accept a typo'd
    # key via setattr, print "set ...", then SILENTLY DROP it on save (asdict ignores unknown attrs).
    if leaf:
        sub = getattr(cfg, table, None)
        if not _is_field(cfg, table) or not _is_field(sub, leaf):
            console.print(f"[red]unknown config key: {key}[/]")
            raise typer.Exit(code=1)
        setattr(sub, leaf, value)
    else:
        if not _is_field(cfg, table):
            console.print(f"[red]unknown config key: {key}[/]")
            raise typer.Exit(code=1)
        setattr(cfg, table, value)
    path = cfg_mod.save(cfg)
    console.print(f"set {key} = {value!r} ([dim]{path}[/])")


@app.command()
def probe() -> None:
    """Inspect the running game: PID, install paths, and a live memory-read smoke test.

    Ties the Phase 0 spike into the tool: confirms we can read the live process's memory.
    """
    pid = find_game_pid()
    if pid is None:
        console.print("[yellow]DQXGame.exe is not running.[/] Start it to probe live memory.")
        raise typer.Exit(code=1)
    install = from_live_process(pid)
    console.print(f"pid: [bold]{pid}[/]")
    if install:
        console.print(f"install_root: {install.install_root}")
        console.print(f"prefix: {install.prefix}")
        console.print(f"appid: {install.appid}")

    # Live read smoke test via /proc/<pid>/mem at the PE image base.
    try:
        from .process.memory_linux import LinuxProcessMemory

        mem = LinuxProcessMemory(pid)
        base = mem.module_base("DQXGame.exe")
        head = mem.read(base, 2) if base else b""
        if head == b"MZ":
            console.print(f"[green]memory read OK[/]: 'MZ' header at {hex(base)}")
        else:
            console.print(f"[red]unexpected bytes at image base[/]: {head!r}")
    except Exception as e:  # noqa: BLE001 - smoke test, report anything
        console.print(f"[red]memory read failed:[/] {e}")
        raise typer.Exit(code=1)


def _is_japanese(text: str) -> bool:
    return any(
        "぀" <= c <= "ヿ" or "一" <= c <= "鿿" or "＀" <= c <= "￯"
        for c in text
    )


def _build_translator(cfg: cfg_mod.Config):
    from .translate.db import TranslationCache
    from .translate.glossary import load_glossary
    from .translate.pipeline import Translator
    from .translate.providers import get_provider

    cache = TranslationCache(cfg_mod.CONFIG_DIR / "clarity_cache.db")

    def _provider(name: str):
        try:
            return get_provider(name, model=cfg.translate.claude_model)
        except ValueError as e:
            console.print(f"[yellow]{e}; ignoring.[/]")
            return None

    # Load the proper-noun glossary ONCE from the local snapshot (refreshed by `sync`); never
    # re-downloaded here. A missing/offline glossary loads empty and glossify becomes a no-op.
    glossary = load_glossary(cfg_mod.CONFIG_DIR / "cache", download=False)

    translator = Translator(
        cache,
        sync_provider=_provider(cfg.translate.provider),  # fast, first-view
        upgrade_provider=_provider(cfg.translate.upgrade_provider),  # slow, background upgrade
        romanize_names=cfg.translate.romanize_names,
        batch_size=cfg.translate.batch_size,
        glossary=glossary,
    )
    # Seed the LIVE placeholder names from config. The PLAYER hook can later overwrite these at
    # runtime (apply_names) so name detection applies without a restart; the community lookup reads
    # them off the translator on every call.
    translator.player_name_ja = cfg.translate.player_name_ja
    translator.player_name_en = cfg.translate.player_name_en
    translator.sibling_name_ja = cfg.translate.sibling_name_ja
    translator.sibling_name_en = cfg.translate.sibling_name_en
    return translator


@app.command()
def romanize(text: str) -> None:
    """Romanize Japanese text locally (pykakasi) — quick offline demo, no game needed."""
    from .translate.romanize import is_available
    from .translate.romanize import romanize as _r

    if not is_available():
        console.print("[red]pykakasi not available[/] — pip install pykakasi")
        raise typer.Exit(code=1)
    console.print(f"{text}  ->  [green]{_r(text)}[/]")


@app.command(name="translate-text")
def translate_text(text: str) -> None:
    """Translate one string through the pipeline (cache → provider) — tests the MT provider."""
    cfg = cfg_mod.load()
    translator = _build_translator(cfg)
    hit = translator.lookup(text)
    if hit is not None:
        console.print(f"[dim]cache hit[/]: {hit}")
        return
    prov = translator.sync_provider or translator.upgrade_provider
    if prov is None:
        from .translate.romanize import romanize as _r

        console.print(f"no MT provider set (pure-local). romaji: [green]{_r(text)}[/]\n"
                      "Set translate.provider = googletranslatefree to machine-translate.")
        return
    if not prov.available():
        console.print(f"[red]provider '{prov.name}' not available[/]")
        raise typer.Exit(code=1)
    console.print(f"[dim]translating via {prov.name}…[/]")
    # Route through translate_now so the dev tool exercises the REAL hot path — glossify-on-MT-input
    # and store_if_better — instead of a bare prov.translate() that would skip glossification.
    en = translator.translate_now(text)
    if en is None:
        console.print("[yellow]provider returned no translation[/]")
    else:
        console.print(f"[green]{en}[/]")


@app.command()
def names(
    interval: float = typer.Option(1.0, "--interval", help="Seconds between scans."),
    duration: float = typer.Option(0.0, "--duration", help="Run for N seconds then stop (0 = until Ctrl-C)."),
) -> None:
    """Live-translate player/NPC/chat names in the running game (Ctrl-C to stop)."""
    import threading

    pid = find_game_pid()
    if pid is None:
        console.print("[yellow]DQXGame.exe is not running.[/]")
        raise typer.Exit(code=1)

    from .process.memory_linux import LinuxProcessMemory
    from .runtime import names_loop

    cfg = cfg_mod.load()
    translator = _build_translator(cfg)
    translator.start()
    mem = LinuxProcessMemory(pid)
    stop = threading.Event()
    console.print(f"name loop on pid {pid} (provider: {cfg.translate.provider}). "
                  f"{'running ' + str(duration) + 's' if duration else 'Ctrl-C to stop'}.")
    if duration:
        threading.Timer(duration, stop.set).start()
    stats = None
    try:
        stats = names_loop.run(
            mem, translator, stop=stop, interval=interval,
            on_write=lambda ja, en: console.print(f"  {ja}  ->  [green]{en}[/]"),
        )
    except KeyboardInterrupt:
        stop.set()
    finally:
        translator.stop()
        translator.cache.close()
    if stats:
        console.print(f"\n[bold]{stats.scans} scans, {stats.seen} JA names seen, "
                      f"{stats.written} written.[/]")
        for ja, en in stats.samples:
            console.print(f"  {ja}  ->  [green]{en}[/]")
    console.print("stopped.")


@app.command()
def scan(
    limit: int = typer.Option(20, "--limit", help="Max hits to report per pattern."),
) -> None:
    """Scan the running game for known text patterns (Phase 2 validation).

    Locates name/notice buffers in the live process and reads the Japanese strings at the
    matches, proving the AOB scanner + memory backend work end to end.
    """
    pid = find_game_pid()
    if pid is None:
        console.print("[yellow]DQXGame.exe is not running.[/] Start it to scan live memory.")
        raise typer.Exit(code=1)

    from .process import signatures as sig
    from .process.memory_linux import LinuxProcessMemory

    mem = LinuxProcessMemory(pid)
    base = mem.module_base("DQXGame.exe")
    nregions = len(mem.scannable_regions(data_only=True))
    console.print(f"pid {pid}  image base {hex(base) if base else '?'}  "
                  f"scanning {nregions} data regions (<2GiB)\n")

    # Engine sanity check, independent of on-screen content: can we find Japanese text at all?
    notice_hits = mem.pattern_scan(sig.NOTICE_STRING, data_only=False, limit=1) or []
    kana_hits = mem.pattern_scan(rb"\xE3\x81\xAE", data_only=True, limit=1000) or []  # 'の'
    if kana_hits:
        console.print(f"[green]engine OK[/]: found {len(kana_hits)}+ Japanese strings in memory"
                      f"{'; login-notice string present' if notice_hits else ''}\n")
    else:
        console.print("[red]engine warning[/]: no Japanese text found — wrong regions or "
                      "the game is still loading.\n")

    table = Table(title="pattern scan")
    table.add_column("pattern")
    table.add_column("hits", justify="right")
    table.add_column("JA", justify="right")
    table.add_column("samples", overflow="fold")
    for np in sig.NAME_PATTERNS:
        addrs = mem.pattern_scan(np.pattern, data_only=True, limit=limit) or []
        samples, ja = [], 0
        for a in addrs:
            s = mem.read_cstring(a + np.name_offset, 64).strip()
            if s and _is_japanese(s):
                ja += 1
                if len(samples) < 4:
                    samples.append(s)
        table.add_row(np.name, str(len(addrs)), str(ja), "  ".join(samples) or "[dim]—[/]")
    console.print(table)
    console.print("[dim]Note: name patterns only match when relevant content is on-screen "
                  "(party/NPCs/chat) and may drift across game patches; the engine-OK line above "
                  "confirms the scanner itself works regardless.[/]")


@app.command(name="hook-dialogue")
def hook_dialogue(
    duration: float = typer.Option(20.0, "--duration", help="Seconds to run before uninstalling."),
    install: bool = typer.Option(
        False, "--install", help="Actually inject the detour (writes code into the game!)."
    ),
) -> None:
    """Phase 3b: native detour hook to capture live dialogue text (capture-only).

    Without --install this only validates (finds the function + a code cave + assembles the
    shellcode) and prints the plan — no memory is written. With --install it injects the detour,
    captures dialogue text pointers for --duration seconds, prints the Japanese it sees, then
    restores the original bytes. INJECTION CAN CRASH THE GAME — use on a backup install.
    """
    import time

    pid = find_game_pid()
    if pid is None:
        console.print("[yellow]DQXGame.exe is not running.[/]")
        raise typer.Exit(code=1)

    from .process import detour, signatures as sig
    from .process.memory_linux import LinuxProcessMemory

    mem = LinuxProcessMemory(pid)
    hits = mem.pattern_scan(sig.DIALOGUE_FUNC, data_only=False, limit=4) or []
    if len(hits) != 1:
        console.print(f"[red]dialogue function: expected 1 match, found {len(hits)}[/] "
                      f"{[hex(a) for a in hits]} — signature may have drifted on this build.")
        raise typer.Exit(code=1)
    func = hits[0]
    cave = detour.find_code_cave(mem, 4 + detour.RING_BYTES + 64)
    console.print(f"dialogue function: [green]{hex(func)}[/]")
    console.print(f"code cave: {hex(cave) if cave else '[red]none found[/]'}")
    if cave is None:
        raise typer.Exit(code=1)

    if not install:
        console.print("[dim]validation only — pass --install to inject and capture.[/]")
        return

    console.print(f"[yellow]injecting detour at {hex(func)} → cave {hex(cave)}…[/]")
    hook = detour.install_capture_hook(mem, func, stolen_len=sig.DIALOGUE_STOLEN_LEN)
    seen: set[str] = set()
    stale = 0
    try:
        deadline = time.time() + duration
        while time.time() < deadline:
            for ptr in hook.poll(mem):
                s = mem.read_cstring(ptr, 512)
                if not s or not _is_japanese(s):
                    stale += 1  # transient/freed buffer read async; ignore
                    continue
                if s not in seen:
                    seen.add(s)
                    console.print(f"  [cyan]{s}[/]")
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        hook.restore(mem)
        console.print(f"[green]restored original bytes.[/] {len(seen)} unique JA lines "
                      f"({stale} stale reads skipped).")


def _suppressions_path() -> Path:
    """Local snapshot of the BAD STRING suppression list (#23), refreshed by `sync`, read by `run`.

    Stored in the SAME config-data directory the TranslationCache (clarity_cache.db) uses, so one
    `dqxclarity sync` refreshes cache + suppressions + reward items in one place.
    """
    return cfg_mod.CONFIG_DIR / "suppressions.json"


def _reward_items_path() -> Path:
    """Local snapshot of the quest-reward item-name dict (#21), refreshed by `sync`, read by `run`.

    Stored alongside clarity_cache.db in the config-data directory (same as the suppressions list).
    """
    return cfg_mod.CONFIG_DIR / "reward_items.json"


@app.command()
def sync() -> None:
    """Download the curated community translation datasets into the cache.

    Pulls ~4k+ human-translated dialogue lines (with <pnplacehold> player-name support) from
    merge.xlsx, then imports the static translation JSON sources (quests, cutscenes, NPC names,
    monsters, items, key items). Covered content then renders instantly and perfectly — machine
    translation is only used for what the community hasn't covered.

    Also refreshes the LOCAL snapshots `run` reads at startup with ZERO network: the BAD STRING
    suppression list (#23) and the quest-reward item-name dict (#21). One `sync` is the single
    network pass; `run` only reads the local cache + these two files.
    """
    import tarfile
    import zipfile

    from .translate.community import (
        fetch_reward_items,
        fetch_suppressions,
        save_reward_items,
        save_suppressions,
        sync_all_static,
        sync_community,
        sync_custom_supplements,
    )
    from .translate.db import TranslationCache
    from .translate.glossary import sync_glossary

    cache = TranslationCache(cfg_mod.CONFIG_DIR / "clarity_cache.db")
    ok = False

    console.print("[dim]downloading community dialogue dataset (merge.xlsx)…[/]")
    try:
        n = sync_community(cache)
        console.print(f"  merge.xlsx (dialogue): [green]{n}[/]")
        ok = True
    except (httpx.HTTPError, OSError) as e:
        console.print(f"  [yellow]merge.xlsx failed: {e}[/]")

    console.print("[dim]downloading the full static corpus (dqx_translations repo)…[/]")
    try:
        files, rows = sync_all_static(cache)
        console.print(f"  static corpus: [green]{rows}[/] from {files} files")
        ok = True
    except (httpx.HTTPError, OSError, tarfile.TarError) as e:
        console.print(f"  [yellow]static corpus failed: {e}[/]")

    console.print("[dim]downloading custom supplements…[/]")
    try:
        sup = sync_custom_supplements(cache)
        console.print(f"  custom supplements: [green]{sup}[/]")
        ok = True
    except (httpx.HTTPError, OSError, zipfile.BadZipFile) as e:
        console.print(f"  [yellow]custom supplements failed: {e}[/]")

    console.print("[dim]downloading proper-noun glossary…[/]")
    try:
        terms = sync_glossary(cfg_mod.CONFIG_DIR / "cache")
        console.print(f"  glossary terms: [green]{terms}[/]")
        ok = True
    except (httpx.HTTPError, OSError) as e:
        console.print(f"  [yellow]glossary failed: {e}[/]")

    # Refresh the two LOCAL snapshots `run` reads at startup (#23 suppressions, #21 reward items),
    # so the fast-startup run path does ZERO network for them. Each is best-effort: a failure here
    # leaves any existing snapshot in place and never aborts the rest of the sync.
    console.print("[dim]downloading bad-string suppressions + quest-reward item dict…[/]")
    n_supp = n_reward = 0
    try:
        supp = fetch_suppressions()
        save_suppressions(_suppressions_path(), supp)
        n_supp = len(supp)
        ok = True
    except (httpx.HTTPError, OSError) as e:
        console.print(f"  [yellow]suppressions failed: {e}[/]")
    try:
        rewards = fetch_reward_items()
        save_reward_items(_reward_items_path(), rewards)
        n_reward = len(rewards)
        ok = True
    except (httpx.HTTPError, OSError, tarfile.TarError, zipfile.BadZipFile) as e:
        console.print(f"  [yellow]reward items failed: {e}[/]")
    console.print(f"  suppressions: [green]{n_supp}[/], reward items: [green]{n_reward}[/]")

    if not ok:
        console.print("[red]sync failed: no sources could be downloaded.[/]")
        cache.close()
        raise typer.Exit(code=1)
    console.print(f"[bold]{len(cache)}[/] total translations in cache.")
    cache.close()


@app.command(name="import-translations")
def import_translations(
    db_path: str = typer.Argument(..., help="Path to a community clarity_dialog.db."),
) -> None:
    """Import human community translations (dialog/quests/walkthrough) into the local cache.

    These are already formatted for the game, so covered dialogue renders perfectly and
    instantly — machine translation is only used for what the community hasn't covered.
    """
    import sqlite3

    from .translate.db import TranslationCache

    src = sqlite3.connect(db_path)
    rows: list[tuple[str, str, str]] = []
    for table in ("dialog", "quests", "walkthrough"):
        try:
            cur = src.execute(
                f"SELECT ja, en FROM {table} WHERE en IS NOT NULL AND en != ''"  # noqa: S608
            )
        except sqlite3.OperationalError:
            continue
        for ja, en in cur:
            if ja and en:
                rows.append((ja, en, "community"))
    src.close()
    cache = TranslationCache(cfg_mod.CONFIG_DIR / "clarity_cache.db")
    cache.store_many(rows)
    console.print(f"imported [green]{len(rows)}[/] community translations "
                  f"({len(cache)} total in cache).")
    cache.close()


@app.command(name="translate-dialogue")
def translate_dialogue(
    duration: float = typer.Option(60.0, "--duration", help="Seconds to run before restoring."),
) -> None:
    """Phase 3b: blocking detour that translates dialogue in-place (writes EN before render).

    Installs the blocking hook, then serves requests in a tight loop: read the JA line, look it
    up in the cache (instant) and write EN back; on a miss, queue it for background MT (so the
    game thread is never held for a slow translation) and leave JA this pass. The same line shows
    EN once cached. Restores original bytes on exit. INJECTION CAN CRASH THE GAME — backup install.
    """
    import time

    pid = find_game_pid()
    if pid is None:
        console.print("[yellow]DQXGame.exe is not running.[/]")
        raise typer.Exit(code=1)

    from .process import detour, signatures as sig
    from .process.memory_linux import LinuxProcessMemory

    mem = LinuxProcessMemory(pid)
    hits = mem.pattern_scan(sig.DIALOGUE_FUNC, data_only=False, limit=4) or []
    if len(hits) != 1:
        console.print(f"[red]dialogue function: found {len(hits)} matches[/] — signature drift?")
        raise typer.Exit(code=1)
    func = hits[0]

    # Recover any detours left patched by a PREVIOUS unclean exit before we install our own. The
    # journal's pid guard prevents writing into a different process that reused the old pid.
    restored = hookjournal.recover_orphans(mem, pid)
    if restored:
        console.print(f"  [yellow]cleaned {len(restored)} orphaned hook(s) from a previous "
                      f"unclean exit[/]")

    cfg = cfg_mod.load()
    translator = _build_translator(cfg)
    translator.start()

    from .runtime.dispatch import build_translate_fn

    tfn, community_lookup = build_translate_fn(cfg, translator)

    console.print(f"[yellow]installing blocking hook at {hex(func)} (provider: "
                  f"{cfg.translate.provider}). talk to NPCs; Ctrl-C / {duration}s to stop.[/]")
    hook = detour.install_blocking_hook(mem, func, stolen_len=sig.DIALOGUE_STOLEN_LEN)
    served = hits_en = 0
    # Same orphan-safety as `run`: hook_session journals the active hook, installs SIGTERM/SIGHUP
    # handlers (so `kill`/terminal-close restore gracefully — a bare SIGTERM would otherwise bypass
    # the finally and orphan the detour), and on exit restores the hook + handlers and clears the
    # journal on success. We also honour `stop` so a signal breaks the deadline loop promptly.
    try:
        with hookjournal.hook_session(mem, pid, [hook], console=console) as stop:
            deadline = time.time() + duration
            while time.time() < deadline and not stop.is_set():
                ja = hook.serve_once(mem, tfn)  # tfn already translated + wrote back
                if ja is not None:
                    served += 1
                    if community_lookup(ja):  # cheap cache check, just for the label
                        hits_en += 1
                        console.print(f"  [green]COMMUNITY[/] {ja.splitlines()[0][:50]!r}")
                    else:
                        hits_en += 1
                        console.print(f"  [cyan]MT[/] {ja.splitlines()[0][:50]!r}")
                # tight poll: only sleep when idle to keep block latency low
                else:
                    time.sleep(0.001)
    except KeyboardInterrupt:
        pass
    finally:
        # The CM already restored the hook + signal handlers and the journal; tear down this
        # command's own translator/cache outside the session.
        translator.stop()
        translator.cache.close()
        console.print(f"[green]restored.[/] served {served} dialogue calls, {hits_en} shown in EN.")


@app.command()
def run(
    hooks: str = typer.Option(
        "dialogue,quest", "--hooks",
        help="Comma-separated surfaces. Text: dialogue, quest, walkthrough, corner_text, "
             "nameplates, network_text. Add 'player' to auto-detect your + your sibling's name "
             "from the login struct (read-only) so name<->placeholder matching works without "
             "manual config (e.g. --hooks dialogue,quest,player).",
    ),
    duration: float = typer.Option(0.0, "--duration", help="Run N seconds (0 = until Ctrl-C)."),
    patch: bool = typer.Option(
        True, "--patch/--no-patch",
        help="On startup, reapply static file patches when the game is closed (then wait for it "
             "to launch). Requires patch.auto_apply in config. --no-patch skips this and fails "
             "fast if the game isn't running.",
    ),
) -> None:
    """Live-translate all enabled text surfaces (dialogue, quests, …) in the running game.

    Installs a native blocking detour per surface and drives one serve loop: each intercepted
    string is resolved community-first (human, instant) then machine-translated, and English is
    written back before it renders. Restores all original bytes on exit.

    Startup ritual (when patch.auto_apply is on and --patch is in effect): if the game is closed,
    reapply static file patches first, then wait for DQXGame.exe to launch before attaching. If
    the game is already running, patching is skipped (unsafe on live files) — a staleness warning
    is printed instead — and we attach immediately. Launching the game first still works.
    """
    import threading

    cfg = cfg_mod.load()

    from .process import hooks as hookmod
    from .process.memory_linux import LinuxProcessMemory
    from .runtime.dispatch import (
        build_name_translate_fn,
        build_network_translate_fn,
        build_quest_translate_fn,
        build_translate_fn,
        serve,
    )
    from .runtime.playernames import build_apply_names
    from .translate.community import load_reward_items_local, load_suppressions_local
    from .translate.suppression import SuppressionIndex

    # --- PID-INDEPENDENT setup, built ONCE and reused across every (re-)attach ----------------- #
    # The translator, suppression index and reward dict depend only on cfg + the LOCAL snapshots
    # `sync` writes — NOT on the game pid — so they survive a game close/restart unchanged. Building
    # them once (and starting the translator once) means a re-attach is cheap and never re-opens the
    # cache or re-loads the network/local data.
    translator = _build_translator(cfg)
    translator.start()

    # Build the cross-surface resources ONCE, from the LOCAL snapshots `sync` writes — run() does
    # ZERO network for either feature (fast-startup contract):
    #   * suppression_index (#23): the BAD STRING pre-pass shared by every prose/text surface — it
    #     returns the curated EN fallback BEFORE the cache/MT for a known-broken machine input.
    #   * reward_items (#21): the JA item-name -> EN item-name dict the quest hook's reward-field
    #     router uses to re-format its STRUCTURED reward lines (never via MT).
    # The local readers never touch the network and never raise: a missing/empty snapshot degrades
    # exactly as before (empty SuppressionIndex; empty reward dict so reward fields fall back to the
    # normal whole-string path). The suppression index is built even when empty so the pre-pass
    # wiring stays uniform; reward_items defaults to {} so build_quest_translate_fn still produces a
    # valid router. When a needed snapshot is missing we print a one-line hint to run `sync`.
    names = [h.strip() for h in hooks.split(",") if h.strip()]
    # Only a hook that declares reward fields (the quest hook) consumes the reward-item snapshot, so
    # skip that local read entirely when no such surface was requested. reward_field_indices is a
    # STATIC property of the HookSpec registry, so this is decided once here from the requested names
    # (no pid / no located hooks needed) and reused unchanged across every (re-)attach.
    wants_reward_items = any(
        hookmod.HOOKS.get(n) is not None and hookmod.HOOKS[n].reward_field_indices for n in names
    )

    suppressions = load_suppressions_local(_suppressions_path())
    suppression_index = SuppressionIndex(suppressions)
    reward_items: dict[str, str] = (
        load_reward_items_local(_reward_items_path()) if wants_reward_items else {}
    )

    def _build_fn(spec):
        """Build the per-surface callback for ``spec`` (PID-independent; see the per-spec comments).

        Returns a plain callable / FieldRouter / apply-names wrapper from cfg + translator +
        the once-loaded reward_items/suppression_index. Called fresh for each freshly-installed
        hook on every (re-)attach — but only the *hook* is pid-bound; this fn is not.
        """
        if spec.player:
            # apply_names updates the live translator + config and returns (player_en, sibling_en)
            # ONLY on a real change (else None). We wrap it solely to console.print the detected EN
            # names exactly once when that change happens — serve_once only surfaces this hook
            # (returns non-None) when apply_names reported a change, so the print here fires once per
            # real detection and never on the idempotent repeats.
            _apply = build_apply_names(cfg, translator)

            def fn(player_ja, sibling_ja, relationship, _apply=_apply):
                result = _apply(player_ja, sibling_ja, relationship)
                if result:
                    pen, sen = result
                    console.print(
                        f"  [green]detected player[/]: {pen or player_ja}"
                        + (f", sibling: {sen or sibling_ja}" if sibling_ja else "")
                    )
                return result

            return fn
        if spec.return_hook:
            # Return-hook surfaces (network_text) get a CATEGORY-AWARE 2-arg fn(ja, category).
            return build_network_translate_fn(
                cfg, translator, wrap_width=spec.wrap_width,
                lines_per_page=spec.lines_per_page, sync=spec.sync,
            )
        if spec.is_name:
            # The nameplates surface (overhead names) needs the \x04 prefix on every replacement:
            # ported from upstream app/hooking/hooks/nameplates.py:54 ("\x04" + result) — without it
            # a replaced overhead name renders RED with a GM-avatar chat picture. The prefix goes on
            # the WRITTEN value only, never the lookup key, and only when a replacement is produced.
            return build_name_translate_fn(cfg, translator, prefix="\x04")
        if spec.reward_field_indices:
            # The quest hook (#21): its STRUCTURED reward fields (indices 3/4) must be cleaned
            # per-line with the item dict, while its prose fields (name/description) keep the normal
            # whole-string path. build_quest_translate_fn returns a FieldRouter that serve_once
            # duck-types (fn_for) to apply the right fn per field. The prose default also carries the
            # BAD STRING suppression pre-pass (#23).
            return build_quest_translate_fn(
                cfg, translator, reward_field_indices=spec.reward_field_indices,
                items_dict=reward_items, wrap_width=spec.wrap_width,
                lines_per_page=spec.lines_per_page, sync=spec.sync,
                suppression=suppression_index,
            )
        fn, _ = build_translate_fn(
            cfg, translator, wrap_width=spec.wrap_width,
            lines_per_page=spec.lines_per_page, sync=spec.sync,
            suppression=suppression_index,
        )
        return fn

    if not suppressions or (wants_reward_items and not reward_items):
        console.print(
            "  [dim]Tip: run `dqxclarity sync` to enable bad-string suppression and "
            "quest-reward cleanup.[/]"
        )

    # --- SUPERVISORY RE-ATTACH LOOP ------------------------------------------------------------ #
    # Each iteration attaches to the CURRENT game pid for the lifetime of one session, then:
    #   * game closed/crashed mid-session -> serve() returns with game_gone set; we re-attach when
    #     the game comes back (a brand-new pid). No patching on re-attach (the game is mid-restart).
    #   * user quit (Ctrl-C) or a SIGTERM/SIGHUP flips stop without game_gone -> we BREAK and exit.
    # hook_session wraps EVERY attached session (the orphan-safety guarantee), so each session's
    # hooks are journalled + restored on exit exactly as before.
    user_quit = False
    first_iteration = True
    total_served = 0
    try:
        while True:
            if first_iteration:
                # First attach keeps the original startup ritual: patch (only when patch+auto_apply)
                # then wait for the game. DECOUPLED: the wait happens REGARDLESS of patching, so
                # --no-patch / auto_apply=False now skips the patch step but STILL waits for the game
                # (no more fail-fast). The patch step is unsafe on a live/mid-restart game, so it is
                # gated to the first attach only.
                if patch and cfg.patch.auto_apply:
                    _apply_patches_for_run(cfg)
                # Resolve the pid with a SINGLE probe: a second find_game_pid() would race the game
                # exiting between check and fetch, handing LinuxProcessMemory(None) a None pid.
                pid = find_game_pid()
                if pid is None:
                    pid = _wait_for_game()
                first_iteration = False
            else:
                # RE-ATTACH after a game-gone: the game just closed / is mid-restart, so NEVER patch
                # (unsafe on live files). Just wait for the new pid to appear.
                pid = _wait_for_game()

            mem = LinuxProcessMemory(pid)
            # Recover any detours left patched by a PREVIOUS unclean exit (SIGKILL/crash) before we
            # touch anything. The journal's pid guards against writing into a different process.
            restored = hookjournal.recover_orphans(mem, pid)
            if restored:
                console.print(f"  [yellow]cleaned {len(restored)} orphaned hook(s) from a previous "
                              f"unclean exit[/]")

            found = hookmod.locate(mem, names)
            resolved = {f.spec.name for f in found}
            for missing in [n for n in names if n not in resolved]:
                console.print(f"  [yellow]{missing}: function not found (signature drift?)[/]")

            # Install hooks for THIS mem and re-pair each freshly-installed (pid-bound) hook with its
            # pid-independent fn. Each hook gets its OWN translate_fn from its HookSpec's format
            # profile so the quest menu isn't paginated/blocked like the dialogue box.
            installed: list[tuple[str, object, object]] = []
            for fh in found:
                try:
                    hook = hookmod.install(mem, fh)
                    installed.append((fh.spec.name, hook, _build_fn(fh.spec)))
                    console.print(f"  hooked [green]{fh.spec.name}[/] @ {hex(fh.func_addr)}")
                except RuntimeError as e:
                    console.print(f"  [yellow]{fh.spec.name}: {e}[/]")
            if not installed:
                console.print("[red]no hooks installed.[/]")
                raise typer.Exit(code=1)

            # hook_session owns the full orphan-safety lifecycle (see its docstring): it persists the
            # crash-recovery journal AFTER install, installs SIGTERM/SIGHUP handlers that only flip
            # `stop` (so `kill`/terminal-close/systemd-stop restore gracefully like Ctrl-C), and on
            # exit restores EVERY hook even if one fails, restores the original signal handlers, then
            # clears the journal only on a full success.
            session_hooks = [hook for _n, hook, _f in installed]
            console.print(f"translating {len(installed)} surface(s) on pid {pid}. Ctrl-C to stop.")
            game_gone = threading.Event()
            with hookjournal.hook_session(mem, pid, session_hooks, console=console) as stop:
                if duration:
                    # duration stops the WHOLE service (not just one attach): flipping stop WITHOUT
                    # game_gone makes the post-with check treat it like a user quit -> break + exit.
                    threading.Timer(duration, stop.set).start()
                try:
                    total_served += serve(
                        mem, installed, stop=stop, game_gone=game_gone,
                        on_line=lambda name, ja: console.print(
                            f"  [dim]{name}[/] {ja.splitlines()[0][:60]!r}"
                        ),
                    )
                except KeyboardInterrupt:
                    user_quit = True
                    stop.set()
            # hook_session's finally has already restored THIS session's hooks + journal.
            # Re-attach ONLY for a genuine game-gone. A SIGTERM/SIGHUP that lands in the SAME tick
            # the game vanished sets stop.signaled (via hook_session) even though game_gone is also
            # set — honour the signal and EXIT rather than looping, so a single `kill <pid>` always
            # stops the service.
            if game_gone.is_set() and not user_quit and not getattr(stop, "signaled", False):
                console.print("[yellow]game closed — waiting for it to return…[/]")
                continue  # re-attach to the next pid
            break  # user quit (Ctrl-C), a duration stop, or a SIGTERM/SIGHUP -> exit
    finally:
        # The CM has restored every session's hooks (and journal); the translator/cache are this
        # command's own PID-INDEPENDENT resources, torn down ONCE after the supervisory loop.
        translator.stop()
        translator.cache.close()
    console.print(f"[green]restored.[/] served {total_served} text fields.")


@app.command()
def clean() -> None:
    """Restore any function prologues left detoured by a previous unclean exit (orphaned hooks).

    A SIGKILL or crash kills `run` without restoring its detours, so the game keeps spinning the
    injected shellcode (a multi-second stall per call that looks like a freeze). This finds the
    running game and restores any orphaned prologues recorded in the hook journal. The pid is
    checked so we never write into a different process that has reused the old pid.
    """
    pid = find_game_pid()
    if pid is None:
        # No game to clean; the journal (if any) is now irrelevant — its addresses belonged to a
        # process that's gone, so drop it without writing anything.
        hookjournal.clear_journal()
        console.print("[yellow]DQXGame.exe is not running.[/] Cleared any stale hook journal; "
                      "nothing to restore.")
        return

    from .process.memory_linux import LinuxProcessMemory

    mem = LinuxProcessMemory(pid)
    restored = hookjournal.recover_orphans(mem, pid)
    if restored:
        console.print(f"[green]restored {len(restored)} orphaned hook(s)[/] on pid {pid}.")
    else:
        console.print("nothing to clean.")


if __name__ == "__main__":
    app()
