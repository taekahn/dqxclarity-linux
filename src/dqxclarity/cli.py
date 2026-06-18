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

# Default surfaces for a bare `dqxclarity run` — ALL of them (the README documents `run` as
# "live-translate all surfaces at once"). This list must grow whenever a new hook surface is added;
# leaving it stale silently disables surfaces for everyone who doesn't pass --hooks. 'player' is
# included so name<->placeholder substitution + the name scanner get the live player/sibling names.
DEFAULT_HOOKS = "dialogue,quest,walkthrough,corner_text,nameplates,network_text,player"

# How long a fresh attach keeps retrying to LOCATE the requested hook functions when the first scan
# finds NONE — the early-attach total miss (#31): the game is still loading and a function's code page
# isn't resolvable yet. Retrying until it settles lets us hook at the earliest possible moment (e.g. to
# catch login-time traffic). Tests set this to 0 to skip the wait.
HOOK_LOCATE_RETRY_SECS = 30.0


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

    upgrade_provider = _provider(cfg.translate.upgrade_provider)  # slow, background upgrade
    # The "claude" alias auto-resolves api->cli and returns None (with a RuntimeWarning) when neither
    # an ANTHROPIC_API_KEY nor a `claude` CLI is available. Surface that as a visible service-log line.
    if upgrade_provider is None and cfg.translate.upgrade_provider == "claude":
        console.print(
            "[yellow]Claude upgrade unavailable: set ANTHROPIC_API_KEY or install the claude CLI.[/]"
        )

    translator = Translator(
        cache,
        sync_provider=_provider(cfg.translate.provider),  # fast, first-view
        upgrade_provider=upgrade_provider,
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


def _set_chat_length(mem, text_addr: int, length: int) -> bool:
    """Set the edit-control LENGTH (and caret) for the chat buffer at ``text_addr`` to ``length``.

    The game sends the input up to its LENGTH field, NOT the NUL terminator (validated live: with a
    stale length the send truncates at the sentinel's length). The edit-control object holds a pointer
    to the text buffer; we find it by reverse-scanning writable memory for a u32 == ``text_addr`` and
    checking the ``0xFFFFFFFF`` anchor at ``CHAT_EDIT_ANCHOR_OFFSET``. On a match we write ``length``
    to the LENGTH and CARET fields (caret -> end). Returns True if a length field was set.

    Only the main (largest) buffer has an edit-control; the smaller render copies don't, so a "not
    found" here is normal for those — the caller sets length only where it can.
    """
    import re
    import struct

    from .process import signatures as sig

    # pattern_scan compiles its argument as a byte-regex, so a LITERAL search must be re.escape'd —
    # a packed address can contain regex metacharacters (e.g. 0x5B '[') that would otherwise raise.
    needle = re.escape(struct.pack("<I", text_addr))
    refs = mem.pattern_scan(needle, data_only=True, return_multiple=True) or []
    set_any = False
    for r in refs:
        try:
            if mem.read_u32(r + sig.CHAT_EDIT_ANCHOR_OFFSET) != sig.CHAT_EDIT_ANCHOR_VALUE:
                continue
            mem.write(r + sig.CHAT_EDIT_LENGTH_OFFSET, struct.pack("<I", length))  # send reads this
            mem.write(r + sig.CHAT_EDIT_CARET_OFFSET, struct.pack("<I", length))   # caret -> end
            set_any = True
        except OSError:
            continue
    return set_any


def _inject_chat_text(mem, text: str, sentinel: str) -> tuple[int, list[tuple[int, int]], int, int]:
    """Find every live chat-input buffer holding ``sentinel``, overwrite it with ``text``, and set
    the edit-control length so ``text`` sends in full.

    This is the pure, game-free core of the ``send-text`` command (so it's unit-testable without
    typer). The mechanism (reverse-engineered + validated live by hand, NOT discovered here):

      * DQX stores the chat input as a null-terminated UTF-8 string with the 4-byte string-object
        header ``40 44 D8 02`` (CHAT_STRING_HEADER) immediately before the text, and a u32 capacity
        field immediately before THAT header. Layout: ``[capacity:u32][40 44 D8 02][utf-8 text][00]``.
        So for a header at H: text starts at H+4 and capacity is read at H-4.
      * The buffer is REALLOCATED every time the chat box opens, and a write only DISPLAYS while the
        box is open and actively editing that buffer — so we can't cache an address or rely on a
        static signature. Instead the user opens chat and types a short ASCII SENTINEL they CAN type
        (no IME needed); we pattern_scan for ``CHAT_STRING_HEADER + sentinel`` to find the live
        buffer(s) and overwrite the text in place. The sentinel's LENGTH is irrelevant to the message
        — it only needs to be findable; the message may be longer (up to the buffer capacity).
      * There are typically 2-3 live copies with different capacities (e.g. 192/128/32). We write to
        EVERY matching copy whose capacity is large enough (``len(utf8)+1 <= cap``) and SKIP any that
        is too small (never overflow). The main input is the 192-cap one, but writing all that fit is
        robust against not knowing which copy the renderer reads.
      * CRUCIAL: the game sends up to the edit-control's LENGTH field, not the NUL — so after writing
        we set that length (and caret) to ``len(utf8)`` via ``_set_chat_length``, else send truncates
        at the sentinel's length. The user presses Enter themselves (we never simulate the send).

    Before writing each match we re-verify the current text still startswith the sentinel, guarding
    against clobbering an unrelated buffer that merely happened to match the header+sentinel bytes
    transiently. Returns ``(written_count, skipped, max_cap, length_set_count)`` where ``skipped`` is a
    list of ``(capacity, needed)`` pairs for too-small copies, ``max_cap`` is the largest capacity
    WRITTEN (0 if none), and ``length_set_count`` is how many buffers got their send-length set.
    """
    import re

    from .process import signatures as sig

    utf8 = text.encode("utf-8")
    text_len = len(utf8)
    needed = text_len + 1  # text + NUL
    sentinel_bytes = sentinel.encode("ascii")

    # re.escape the LITERAL header+sentinel: pattern_scan treats its arg as a byte-regex, so a sentinel
    # containing a regex metacharacter (".", "[", "*", …) would otherwise mis-scan or raise.
    needle = re.escape(sig.CHAT_STRING_HEADER + sentinel_bytes)
    hits = mem.pattern_scan(needle, return_multiple=True) or []

    written = 0
    skipped: list[tuple[int, int]] = []
    max_cap = 0
    length_set = 0
    for header in hits:
        text_addr = header + 4
        try:
            cap = mem.read_u32(header - 4)
            current = mem.read_cstring(text_addr, max(needed, len(sentinel_bytes) + 8))
        except OSError:
            # A buffer can be freed/remapped between the scan and the read; a transient read error on
            # one match must not abort the whole send — skip it and try the others.
            continue
        # Guard: only overwrite a buffer that STILL holds the sentinel we matched. A header match whose
        # text no longer starts with the sentinel is stale/unrelated — never clobber it.
        if not current.startswith(sentinel):
            continue
        if needed <= cap:
            try:
                if mem.write_cstring(text_addr, text, max_bytes=cap):
                    written += 1
                    max_cap = max(max_cap, cap)
                    if _set_chat_length(mem, text_addr, text_len):
                        length_set += 1
            except OSError:
                continue
        else:
            skipped.append((cap, needed))
    return written, skipped, max_cap, length_set


@app.command(name="send-text")
def send_text(
    text: str = typer.Argument(..., help="Text to inject into the open chat box (any language)."),
    sentinel: str = typer.Option(
        "qzx", "--sentinel",
        help="The ASCII placeholder you typed in the chat box; the tool finds that buffer and "
             "replaces it.",
    ),
) -> None:
    """Inject arbitrary text into the game's OPEN chat input box (no Linux Japanese IME needed).

    DQX reallocates the chat-input buffer every time the box opens and only renders writes to the
    buffer it's actively editing, so we can't cache an address. Instead: open the chat box, type a
    short ASCII SENTINEL you CAN type (default 'qzx') and nothing else, leave it there, then run this
    command. It scans the live process for the chat buffer(s) holding that sentinel and overwrites
    the text in place (UTF-8, all copies that fit; too-small copies are skipped, never overflowed) and
    sets the edit-control LENGTH so the whole message sends (the game sends up to that length, not the
    NUL). The sentinel's length doesn't matter — type a short one, send any message up to the buffer
    capacity. Press Enter in-game yourself to send.
    """
    pid = find_game_pid()
    if pid is None:
        console.print("[yellow]DQXGame.exe is not running.[/] Start it and open the chat box first.")
        raise typer.Exit(code=1)

    # The sentinel must be something the user can actually TYPE without an IME, so it has to be ASCII.
    if not sentinel or not sentinel.isascii():
        console.print(f"[red]sentinel must be ASCII you can type without an IME[/] (got {sentinel!r}).")
        raise typer.Exit(code=1)

    from .process.memory_linux import LinuxProcessMemory

    mem = LinuxProcessMemory(pid)
    try:
        written, skipped, max_cap, length_set = _inject_chat_text(mem, text, sentinel)
    except OSError as e:
        # A transient process_vm_readv/writev failure (region remapped mid-scan) — report cleanly
        # rather than dumping a traceback; the user can simply re-run.
        console.print(f"[red]memory access failed:[/] {e} — re-run with the chat box still open.")
        raise typer.Exit(code=1)

    if written == 0 and not skipped:
        console.print(
            f"[yellow]No chat buffer holding '{sentinel}' found.[/] Open the chat input box, type "
            f"{sentinel} (and nothing else), leave it there, then re-run."
        )
        raise typer.Exit(code=1)

    if written == 0:
        # Every matching copy was too small — the text is longer than the chat buffer can hold. The
        # main input buffer is 192 bytes (~63 Japanese chars), so report that ceiling.
        biggest = max(cap for cap, _ in skipped)
        console.print(
            f"[red]Text too long for the chat buffer[/] (largest capacity {biggest} bytes, "
            f"~{biggest // 3} JA chars). Nothing written — shorten the text and re-run."
        )
        raise typer.Exit(code=1)

    note = f" ({len(skipped)} smaller copy skipped)" if skipped else ""
    console.print(
        f"[green]injected into {written} chat buffer(s)[/] (max capacity {max_cap} bytes){note}. "
        "Now press Enter in-game to send."
    )
    if length_set == 0:
        # We wrote the text but couldn't find/set the edit-control length — the game may send only up
        # to the sentinel's length (truncated). Surface it rather than letting a partial message post.
        console.print(
            "[yellow]warning:[/] could not set the send-length (edit-control not found) — the send "
            "may truncate to the sentinel's length. Re-run with the chat box freshly open."
        )


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


def _run_sync(cfg: cfg_mod.Config) -> bool:
    """Download the community + static datasets and refresh the local run-time snapshots.

    This is the shared body behind the `sync` command AND `run`'s staleness-gated auto-refresh.
    Each sub-step is best-effort (prints a warning and continues on its own network/IO error). When
    at least one source downloaded, stamps the `last_sync` freshness marker and returns True; when
    NOTHING ran (every source failed), the marker is NOT stamped and False is returned.
    """
    import tarfile
    import zipfile

    from .translate import freshness
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
        return False
    console.print(f"[bold]{len(cache)}[/] total translations in cache.")
    cache.close()
    # At least one source ran: stamp the LOCAL freshness marker so `run`'s staleness check stays
    # network-free until it next goes stale (warnings on best-effort sub-steps don't unset this).
    freshness.mark_synced()
    return True


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
    cfg = cfg_mod.load()
    if not _run_sync(cfg):
        raise typer.Exit(code=1)


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


@app.command()
def run(
    hooks: str = typer.Option(
        DEFAULT_HOOKS, "--hooks",
        help="Comma-separated surfaces. Default = ALL: dialogue, quest, walkthrough, corner_text, "
             "nameplates, network_text, player. 'player' auto-detects your + your sibling's name "
             "from the login struct (read-only) so name<->placeholder matching works without manual "
             "config. Pass a subset to translate only specific surfaces (e.g. --hooks dialogue).",
    ),
    duration: float = typer.Option(0.0, "--duration", help="Run N seconds (0 = until Ctrl-C)."),
    patch: bool = typer.Option(
        True, "--patch/--no-patch",
        help="On startup, reapply static file patches when the game is closed (then wait for it "
             "to launch). Requires patch.auto_apply in config. --no-patch skips this and fails "
             "fast if the game isn't running.",
    ),
    sync: bool = typer.Option(
        True, "--sync/--no-sync",
        help="On startup, auto-refresh the translation DB when it's STALE or never synced (a "
             "purely LOCAL check — a fresh DB does zero network). Requires translate.auto_sync. "
             "--no-sync skips it; a stale/empty cache never aborts run.",
    ),
    capture_network: Path | None = typer.Option(
        None, "--capture-network",
        help="Capture ALL network_text traffic (category + text) to a JSON report instead of "
             "translating that surface — for tiering analysis. Low-lag pure-observe; dumps on exit.",
    ),
    names: bool = typer.Option(
        True, "--names/--no-names",
        help="Master toggle for live NAME translation alongside the hooks. On by default: names come "
             "from cheap POINTER CHAINS where we have one (currently the party-panel name), with "
             "no memory scanning. Add --name-scan for the old intrusive AOB scanner (concierge/chat). "
             "SEPARATE from the 'player' hook (which auto-detects YOUR + your sibling's name from the "
             "login struct). --no-names disables ALL name translation (chains and scanner).",
    ),
    name_scan: bool = typer.Option(
        False, "--name-scan/--no-name-scan",
        help="Use the INTRUSIVE memory-scan name translator (AOB-sweeps ~1GB/tick to find concierge/"
             "party/chat names; causes periodic microstutter). Default OFF: names come from cheap "
             "pointer chains where we have them, and uncovered kinds are skipped with a warning.",
    ),
    name_patterns: str = typer.Option(
        "concierge,party,chat", "--name-patterns",
        help="Which of the scanned name kinds to translate, comma-separated: concierge, party "
             "(menu AI), chat. Default = all three. Subset to drop the dynamic chat scan or to "
             "isolate one (e.g. --name-patterns party). --no-names disables all of them.",
    ),
    names_interval: float = typer.Option(
        1.0, "--names-interval", help="Seconds between name-scanner passes (with --names)."
    ),
    notice: bool = typer.Option(
        False, "--notice/--no-notice",
        help="EXPERIMENTAL / OFF by default (see #27): run the polling NOTICE scanner that tries to "
             "live-translate the startup 'Important Notice' body by writing English into its memory "
             "buffer. DISABLED because the current anchor is wrong: the notice is rendered from a "
             "VOLATILE, per-page buffer (only the displayed page is resident; the page-2 anchor it "
             "keys on isn't present on page 1) that the game continuously re-populates, so the writes "
             "don't reach the rendered copy AND poking those buffers can crash the game. Needs a "
             "code-hook redo before re-enabling. --notice opts in at your own risk.",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v",
        help="Log every intercepted line per surface (and each name/notice write) — a noisy "
             "diagnostic. Default is QUIET: the startup summary plus key events (game closed, "
             "errors, exit) only.",
    ),
    profile: bool = typer.Option(
        False, "--profile",
        help="Diagnose game-thread hitches: time each serve_once (per surface), each name-scanner "
             "pass (warm/full), and serve-loop starvation gaps. Logs slow events (>=30ms) live with "
             "a timestamp so a periodic spike's cadence is visible, and prints a per-component "
             "summary on exit. Use to pinpoint a recurring lag spike.",
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
    from .runtime import name_chains, names_loop, notice_loop
    from .runtime.playernames import build_apply_names
    from .translate.community import load_reward_items_local, load_suppressions_local
    from .translate.suppression import SuppressionIndex

    # --- STALENESS-GATED AUTO-REFRESH (#19) ---------------------------------------------------- #
    # BEFORE building the translator (so the bulk import never contends with the translator's open
    # cache connection): if the LOCAL freshness marker says the DB is stale or was never synced,
    # do a one-time network refresh. The staleness CHECK is purely LOCAL — a fresh DB does ZERO
    # network here and adds zero startup cost. The refresh itself is best-effort: any network/IO
    # error prints a warning and CONTINUES, so a stale or empty cache NEVER aborts run (the user can
    # still play on whatever's cached). --no-sync / translate.auto_sync=False skip the check entirely.
    from .translate import freshness
    if sync and cfg.translate.auto_sync and freshness.is_db_stale(cfg.translate.sync_max_age_days):
        age = freshness.db_age_days()
        if age is None:
            console.print("[dim]translation DB not yet synced — refreshing… (one-time, then cached)[/]")
        else:
            console.print(f"[dim]translation DB {age:.0f} days old — refreshing… "
                          "(one-time, then cached)[/]")
        try:
            _run_sync(cfg)
        except Exception as e:  # noqa: BLE001 - best-effort: auto-sync must NEVER abort the session
            # _run_sync absorbs each source's own network/IO error, but anything escaping it
            # (e.g. a ValueError from a corrupt upstream sheet, or a cache hiccup) must still not
            # take down run — the user keeps playing on whatever the cache already holds.
            console.print(f"[yellow]auto-sync failed ({e}); continuing on the cached DB.[/]")

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
    # NB: `names` (the bool flag) is a SEPARATE thing from this hook-name LIST — keep the list under
    # its own name so the flag stays readable below where we start the scanner.
    hook_names = [h.strip() for h in hooks.split(",") if h.strip()]
    # Only a hook that declares reward fields (the quest hook) consumes the reward-item snapshot, so
    # skip that local read entirely when no such surface was requested. reward_field_indices is a
    # STATIC property of the HookSpec registry, so this is decided once here from the requested names
    # (no pid / no located hooks needed) and reused unchanged across every (re-)attach.
    wants_reward_items = any(
        hookmod.HOOKS.get(n) is not None and hookmod.HOOKS[n].reward_field_indices for n in hook_names
    )

    suppressions = load_suppressions_local(_suppressions_path())
    suppression_index = SuppressionIndex(suppressions)
    reward_items: dict[str, str] = (
        load_reward_items_local(_reward_items_path()) if wants_reward_items else {}
    )

    # --- NETWORK-TEXT CAPTURE MODE (tiering data-gathering) ------------------------------------ #
    # When --capture-network is set, build ONE recorder BEFORE the supervisory loop. Every (re-)attach
    # routes network_text to a pure-observe fn that records (category, ja) and returns None (no MT, no
    # write), accumulating into the SAME recorder across game-gone re-attaches. The dump happens ONCE
    # in the finally below (so Ctrl-C / game-close / duration / normal exit all flush it).
    recorder = None
    # ``isinstance(... Path)`` (not just ``is not None``) so a direct call that leaves the typer
    # default in place (an OptionInfo sentinel, as the lifecycle tests do) is treated as "not set".
    if isinstance(capture_network, Path):
        from .runtime.netcapture import NetworkCaptureRecorder

        recorder = NetworkCaptureRecorder()
        console.print(
            "[yellow]network_text CAPTURE mode[/] — observing only (no translation/write) → "
            f"{capture_network}"
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
            # CAPTURE mode: route network_text to a pure-observe fn that records (category, ja) and
            # returns None — no MT, no write — so a real playthrough's FULL traffic is captured for
            # the tiering decision. Matches the surface's fn(ja, category) signature. Other surfaces
            # (if also requested) keep their normal behaviour.
            if recorder is not None and spec.name == "network_text":
                def capture_fn(ja, category, _rec=recorder):
                    _rec.record(category, ja)
                    return None

                return capture_fn
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
                suppression=suppression_index, surface=spec.name,
            )
        # The remaining prose surfaces (dialogue/walkthrough/corner_text). ``spec.name`` is the
        # register hint threaded to the rich Claude provider so it can match the line's register
        # (the labels documented in _SYSTEM_RICH: "dialogue"/"quest"/etc.).
        fn, _ = build_translate_fn(
            cfg, translator, wrap_width=spec.wrap_width,
            lines_per_page=spec.lines_per_page, sync=spec.sync,
            suppression=suppression_index, surface=spec.name,
        )
        return fn

    if not suppressions or (wants_reward_items and not reward_items):
        console.print(
            "  [dim]Tip: run `dqxclarity sync` to enable bad-string suppression and "
            "quest-reward cleanup.[/]"
        )

    # --- NOTICE SCANNER prose fn (PID-INDEPENDENT, built ONCE) --------------------------------- #
    # The startup "Important Notice" body is a STATIC memory buffer that never flows through any code
    # hook (confirmed absent from a full network_text capture), so it can't be a HookSpec — it's
    # handled by the notice scanner instead (notice_loop). It rides the SAME prose pipeline as
    # dialogue: build_translate_fn (community-first, then MT, placeholder-safe). We build the fn ONCE
    # here (pid-independent, like the translator) and reuse it for every attach. sync=True so the
    # notice translates immediately on the login screen (it's one-shot per appearance, not a hot
    # path); the BAD STRING suppression pre-pass is included for parity with the dialogue path.
    notice_translate_fn, _ = build_translate_fn(
        cfg, translator, wrap_width=notice_loop.NOTICE_WRAP_WIDTH,
        lines_per_page=0,  # no <br> pagination: the notice carries its own literal <PAGE> breaks
        sync=True,  # one-shot off the hot path -> block on MT so it fills on the first tick it's seen
        suppression=suppression_index, surface="notice",
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
    # --profile: one Profiler spanning all attaches (like the translator). Live-logs slow events
    # (>=30ms) with a timestamp so a periodic hitch's cadence is visible; summary printed on exit.
    profile_on = profile if isinstance(profile, bool) else False
    profiler = None
    if profile_on:
        from .runtime.profile import Profiler

        profiler = Profiler(
            on_slow=lambda ts, kind, label, ms, detail: console.print(
                f"[magenta][prof {ts:6.1f}s][/] {kind}:{label} [bold]{ms:.0f}ms[/] {detail}"
            )
        )
        console.print("  [magenta]profiling on[/] — slow events (>=30ms) logged live; summary on exit")
        translator.profiler = profiler  # so the background MT worker (_run) also records mt:<provider>
    # Holds the LAST attach's hooks so the exit profile can read each hook's serviced-request count
    # (initialized here so the finally is safe even if the game never starts).
    installed: list[tuple[str, object, object]] = []
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

            # Locate the requested hook functions, WAITING until the WHOLE set resolves before we
            # install anything (#31). On a cold launch the service attaches the instant DQXGame.exe
            # appears — but the game is still decrypting/relocating its code, so only SOME hook
            # functions are at their final bytes yet. Installing a detour into a not-yet-settled
            # function corrupts it: a partial-hook attach (e.g. 3/7) crashed the game with an access
            # violation EXECUTING at ~null (a clobbered code redirect). Resolving ALL requested hooks
            # is our "game fully loaded" signal — only then is it safe to install. We poll until the
            # set is complete or the window elapses (genuine signature drift -> proceed with whatever
            # resolved + report the missing below). Deadline is checked BEFORE is_alive() so a zero
            # window (tests) skips the probe entirely (the mem stub has no is_alive()).
            import time as _t
            _deadline = _t.monotonic() + HOOK_LOCATE_RETRY_SECS
            found = hookmod.locate(mem, hook_names)
            while len(found) < len(hook_names) and _t.monotonic() < _deadline and mem.is_alive():
                _t.sleep(0.4)
                found = hookmod.locate(mem, hook_names)
            resolved = {f.spec.name for f in found}
            for missing in [n for n in hook_names if n not in resolved]:
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
                # --- NAME SCANNER (per-attach daemon thread) --------------------------------------- #
                # The polling name scanner (concierge/party/chat names) runs ALONGSIDE the hooks for
                # the life of THIS attach. It is hook-free — it scans+writes the per-attach `mem` —
                # and reuses the ONE pid-independent translator built before the loop (never a second
                # translator/cache). It keys off its OWN stop Event, NOT the shared `stop`: a
                # game-gone sets game_gone but NOT `stop`, so a scanner on the shared `stop` would
                # spin against the dead pid until the next attach. start_scanner gives it a private
                # names_stop; we stop+join it in the finally below (covering serve() returning AND a
                # KeyboardInterrupt) BEFORE the loop re-attaches or breaks, so it's fully wound down
                # before the next attach builds a fresh mem. --no-names -> handle with no thread.
                # Sentinel-safe coerce: a direct (non-CLI) cli.run(...) that omits these leaves typer's
                # OptionInfo in place (the CLI resolves them, programmatic callers don't). OptionInfo
                # is truthy but NOT a real number, so float() it would raise — fall back to the declared
                # defaults instead. An un-coerced interval would otherwise blow up at stop.wait(interval).
                names_on = names if isinstance(names, bool) else True
                interval = float(names_interval) if isinstance(names_interval, (int, float)) else 1.0
                # QUIET by default: per-line/per-write logging only with --verbose. Sentinel-safe
                # coerce for direct (non-CLI) cli.run callers that leave typer's OptionInfo in place.
                verbose_on = verbose if isinstance(verbose, bool) else False
                name_scan_on = name_scan if isinstance(name_scan, bool) else False
                name_on_write = (
                    (lambda ja, en: console.print(f"  [dim]name[/] {ja} -> [green]{en}[/]"))
                    if verbose_on else None
                )
                # NAME translation has TWO mutually-exclusive engines under the master `names` toggle:
                #   * --name-scan: the INTRUSIVE AOB scanner (concierge/party/chat) — exactly as before.
                #   * default: the cheap POINTER-CHAIN reader (party where we have a chain), with a
                #     warning that scan-only kinds need --name-scan.
                # --no-names (names_on False) runs NEITHER. Each start helper is called EXACTLY ONCE
                # per attach; the inactive engine gets enabled=False (a no-op handle) so the uniform
                # stop_and_join in the finally stays valid regardless of which engine is live.
                if names_on and name_scan_on:
                    # INTRUSIVE scanner path: --name-patterns subsets concierge/party/chat.
                    np_raw = name_patterns if isinstance(name_patterns, str) else "concierge,party,chat"
                    requested = [t.strip() for t in np_raw.split(",") if t.strip()]
                    selected = names_loop.select_patterns(requested)
                    _known = {np.name for np in names_loop.NAME_PATTERNS}
                    unknown = [r for r in requested
                               if names_loop.PATTERN_ALIASES.get(r.lower(), r.lower()) not in _known]
                    if unknown:
                        console.print(
                            f"  [yellow]ignoring unknown name-patterns: {', '.join(unknown)}[/]"
                        )
                    if selected:
                        active = ", ".join(names_loop.friendly_name(np.name) for np in selected)
                        console.print(f"  name scanner on (intrusive AOB; {active})")
                    scanner = names_loop.start_scanner(
                        mem, translator, enabled=bool(selected), interval=interval,
                        on_write=name_on_write, profiler=profiler, patterns=selected,
                    )
                    chain_reader = name_chains.start_chain_reader(
                        mem, translator, None, enabled=False, interval=interval,
                    )
                elif names_on:
                    # DEFAULT cheap pointer-chain path. Resolve the image base once; the chains are
                    # expressed relative to it. A None base (module not mapped) or a party chain that
                    # doesn't resolve at startup almost always means a game update moved the offsets —
                    # warn and point the user at --name-scan.
                    base = mem.module_base()
                    covered = ", ".join(c.kind for c in name_chains.NAME_CHAINS)
                    console.print(f"  name chains on ({covered})")
                    console.print(
                        f"  [yellow]concierge/chat names need --name-scan (intrusive memory scan); "
                        f"pointer chains cover: {covered}[/]"
                    )
                    chain_ok = base is not None and any(
                        name_chains.resolve_chain(mem, base, c) is not None
                        for c in name_chains.NAME_CHAINS
                    )
                    if not chain_ok:
                        console.print(
                            "  [yellow]name pointer chain didn't resolve (likely a game update) — "
                            "use --name-scan for the intrusive memory-scan name translator.[/]"
                        )
                    # enabled=chain_ok: if not even the always-resident party chain resolves at
                    # startup it's a broken (game-updated) build — don't spin a thread that can only
                    # mark everything broken; the warning above already told the user to use --name-scan.
                    chain_reader = name_chains.start_chain_reader(
                        mem, translator, base, enabled=chain_ok, interval=interval,
                        on_write=name_on_write, profiler=profiler,
                    )
                    scanner = names_loop.start_scanner(
                        mem, translator, enabled=False, interval=interval,
                    )
                else:
                    # --no-names: neither engine runs. Both helpers are still called (uniform call
                    # site) with enabled=False so the finally's stop_and_join has valid no-op handles.
                    scanner = names_loop.start_scanner(
                        mem, translator, enabled=False, interval=interval,
                    )
                    chain_reader = name_chains.start_chain_reader(
                        mem, translator, None, enabled=False, interval=interval,
                    )
                # --- NOTICE SCANNER (per-attach daemon thread) ------------------------------------- #
                # Same per-attach, private-stop lifecycle as the name scanner (see above): the startup
                # "Important Notice" body is a STATIC buffer with no hook, so we scan+translate+write
                # it in place. It reuses the ONE pid-independent notice_translate_fn built before the
                # loop. The scanner is idempotent (it only acts while the buffer reads Japanese), so it
                # translates once per appearance and idles otherwise. --no-notice -> handle, no thread.
                # Sentinel-safe coerce of the typer flag for direct (non-CLI) cli.run callers.
                notice_on = notice if isinstance(notice, bool) else False  # #27: OFF by default
                if notice_on:
                    console.print("  notice scanner on (startup Important Notice)")
                notice_scanner = notice_loop.start_notice_scanner(
                    mem, notice_translate_fn, enabled=notice_on, interval=interval,
                    on_write=(lambda: console.print("  [dim]notice[/] [green]translated[/]"))
                    if verbose_on else None,
                )
                try:
                    total_served += serve(
                        mem, installed, stop=stop, game_gone=game_gone,
                        on_line=(lambda name, ja: console.print(
                            f"  [dim]{name}[/] {ja.splitlines()[0][:60]!r}"
                        )) if verbose_on else None,
                        profiler=profiler,
                    )
                except KeyboardInterrupt:
                    user_quit = True
                    stop.set()
                finally:
                    # Stop+join ALL per-attach readers BEFORE leaving the with-block (and thus before
                    # any re-attach). A short join timeout keeps a stuck scan from wedging exit;
                    # they're daemon threads so even a missed join can't block process shutdown. At
                    # most one of scanner/chain_reader has a live thread; the other is a no-op handle.
                    scanner.stop_and_join(timeout=5.0)
                    chain_reader.stop_and_join(timeout=5.0)
                    notice_scanner.stop_and_join(timeout=5.0)
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
        # CAPTURE dump: fire on EVERY exit path (Ctrl-C / game-close / duration / normal), once,
        # after the loop — game-gone re-attaches accumulated into the SAME recorder above.
        if recorder is not None:
            from .runtime.netcapture import build_summary_table

            report = recorder.report()
            out_path = recorder.dump(capture_network)
            console.print(build_summary_table(report))
            console.print(
                f"[green]captured[/] {report['totals']['calls']} calls across "
                f"{report['totals']['categories']} categories → {out_path}"
            )
        # PROFILE summary: per-component timing + the detected hitch cadence (every exit path).
        if profiler is not None:
            console.print(profiler.summary_table())
            hint = profiler.cadence_hint()
            if hint:
                console.print(f"[magenta]hitch cadence:[/] {hint}")
            else:
                console.print("[dim]no hitches >=30ms recorded.[/]")
            # Per-hook game-side request rate. A BLOCKING hook on a hot (per-frame) function stalls
            # the game on every call regardless of whether it returns text, so a high req/s here —
            # even with no timed serve events — flags the hot hook (e.g. player) the timing table hides.
            elapsed = profiler.elapsed() or 1.0
            hook_rows = sorted(
                ((n, getattr(h, "requests", 0)) for n, h, _ in installed),
                key=lambda r: r[1], reverse=True,
            )
            if hook_rows:  # always print (even all-zero) so an idle hook is explicit, not just absent
                console.print("[magenta]hook request rates[/] (hot blocking hook = per-call game stalls):")
                for name, n in hook_rows:
                    console.print(f"  {name}: {n} reqs ({n / elapsed:.0f}/s)")
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
