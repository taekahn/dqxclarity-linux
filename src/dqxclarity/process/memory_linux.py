"""Linux process memory backend — the native equivalent of pymem.

Reads/writes another process's address space via ``process_vm_readv(2)`` /
``process_vm_writev(2)`` with a ``/proc/<pid>/mem`` fallback, and resolves module base
addresses by parsing ``/proc/<pid>/maps``.

Verified against a live DQXGame.exe under Proton WOW64: reads and writes succeed for a
same-uid process with no elevation, even with ``kernel.yama.ptrace_scope=1``. The game is a
32-bit PE (image base ``0x00400000``); pointer width defaults to 32-bit here.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import re
import struct
from dataclasses import dataclass
from pathlib import Path

# The game is a 32-bit PE; all game addresses live below this (upstream uses the same cap).
USER_ADDR_MAX = 0x7FFFFFFF

_libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)


class _iovec(ctypes.Structure):
    _fields_ = [("iov_base", ctypes.c_void_p), ("iov_len", ctypes.c_size_t)]


for _fn in ("process_vm_readv", "process_vm_writev"):
    _f = getattr(_libc, _fn)
    _f.restype = ctypes.c_ssize_t
    _f.argtypes = [
        ctypes.c_int,
        ctypes.POINTER(_iovec),
        ctypes.c_ulong,
        ctypes.POINTER(_iovec),
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]


@dataclass
class MapRegion:
    start: int
    end: int
    perms: str
    path: str

    @property
    def size(self) -> int:
        return self.end - self.start


_MAPS_RE = re.compile(
    r"^([0-9a-f]+)-([0-9a-f]+) (\S{4}) [0-9a-f]+ \S+ \d+\s*(.*)$"
)


class LinuxProcessMemory:
    def __init__(self, pid: int, *, pointer_width: int = 4) -> None:
        self.pid = pid
        self.pointer_width = pointer_width  # 4 = 32-bit game (DQX), 8 = 64-bit

    # ----- memory I/O ----------------------------------------------------- #
    def read(self, addr: int, size: int) -> bytes:
        """Read up to ``size`` bytes. May return fewer if memory is unmapped near the end.

        process_vm_readv can succeed partially (n < size) at a region boundary; on a short or
        failed read we also try /proc/<pid>/mem and return whichever got more, so callers don't
        silently act on a truncated process_vm_readv result.
        """
        buf = (ctypes.c_char * size)()
        local = _iovec(ctypes.cast(buf, ctypes.c_void_p), size)
        remote = _iovec(ctypes.c_void_p(addr), size)
        n = _libc.process_vm_readv(self.pid, ctypes.byref(local), 1, ctypes.byref(remote), 1, 0)
        if n == size:
            return bytes(buf)
        pvr = bytes(buf[:n]) if n > 0 else b""
        try:
            alt = self._read_procmem(addr, size)
        except OSError:
            alt = b""
        return alt if len(alt) >= len(pvr) else pvr

    def is_alive(self) -> bool:
        """Cheap liveness check: does our target pid still exist (and isn't a zombie)?

        Called when a read fails to tell "the game is GONE" (re-attach territory) apart from a
        transient short/failed read on a still-running process (skip-this-tick territory). The
        ``/proc/<pid>`` existence check is the essential signal; we additionally treat a zombie
        (``/proc/<pid>/stat`` state ``Z``) as not-alive, since a reaped-but-not-yet-collected
        process can't be read from either. Any error reading stat falls back to the path check.
        """
        if not os.path.exists(f"/proc/{self.pid}"):
            return False
        try:
            stat = Path(f"/proc/{self.pid}/stat").read_text()
            # /proc/<pid>/stat: "pid (comm) STATE ..." — comm may contain spaces/parens, so split
            # on the LAST ')' and read the state char that follows.
            state = stat[stat.rfind(")") + 1:].split()[0]
            return state != "Z"
        except (OSError, IndexError):
            return True  # exists but stat unreadable -> trust the path check (alive)

    def write(self, addr: int, data: bytes) -> int:
        buf = (ctypes.c_char * len(data)).from_buffer_copy(data)
        local = _iovec(ctypes.cast(buf, ctypes.c_void_p), len(data))
        remote = _iovec(ctypes.c_void_p(addr), len(data))
        n = _libc.process_vm_writev(self.pid, ctypes.byref(local), 1, ctypes.byref(remote), 1, 0)
        if n < 0:
            return self._write_procmem(addr, data)
        return n

    def _read_procmem(self, addr: int, size: int) -> bytes:
        with open(f"/proc/{self.pid}/mem", "rb", 0) as f:
            f.seek(addr)
            return f.read(size)

    def _write_procmem(self, addr: int, data: bytes) -> int:
        with open(f"/proc/{self.pid}/mem", "rb+", 0) as f:
            f.seek(addr)
            return f.write(data)

    # ----- typed helpers -------------------------------------------------- #
    def read_u32(self, addr: int) -> int:
        return struct.unpack("<I", self.read(addr, 4))[0]

    def read_ptr(self, addr: int) -> int:
        raw = self.read(addr, self.pointer_width)
        return int.from_bytes(raw, "little")

    def read_cstring(self, addr: int, max_len: int = 512, encoding: str = "utf-8") -> str:
        raw = self.read(addr, max_len)
        end = raw.find(b"\x00")
        if end != -1:
            raw = raw[:end]
        return raw.decode(encoding, "replace")

    def write_cstring(self, addr: int, text: str, *, max_bytes: int, encoding: str = "utf-8") -> bool:
        """Write a null-terminated string into a fixed-size buffer.

        Returns False (writing nothing) if ``text`` + NUL doesn't fit in ``max_bytes`` — never
        overflow the game's buffer. The buffer is zero-padded so no stale tail bytes remain.
        """
        data = text.encode(encoding, "replace") + b"\x00"
        if len(data) > max_bytes:
            return False
        data += b"\x00" * (max_bytes - len(data))
        self.write(addr, data)
        return True

    # ----- module / maps -------------------------------------------------- #
    def regions(self) -> list[MapRegion]:
        out: list[MapRegion] = []
        for line in Path(f"/proc/{self.pid}/maps").read_text().splitlines():
            m = _MAPS_RE.match(line)
            if m:
                out.append(
                    MapRegion(int(m.group(1), 16), int(m.group(2), 16), m.group(3), m.group(4))
                )
        return out

    def module_base(self, name: str) -> int | None:
        """Lowest mapped address of the named module (matched on the maps path)."""
        bases = [r.start for r in self.regions() if r.path.endswith(name)]
        return min(bases) if bases else None

    def module_regions(self, name: str) -> list[MapRegion]:
        return [r for r in self.regions() if r.path.endswith(name)]

    def scannable_regions(self, *, data_only: bool = False) -> list[MapRegion]:
        """Readable regions worth scanning, restricted to the 32-bit game address space.

        ``data_only`` keeps only writable data pages (the equivalent of upstream's
        MEM_PRIVATE/MEM_MAPPED + read/write filter), excluding executable code and read-only
        image data. Skips kernel pseudo-regions (vdso/vvar/vsyscall) and anything ≥ 2 GiB.
        """
        skip = {"[vdso]", "[vvar]", "[vsyscall]", "[vectors]"}
        out: list[MapRegion] = []
        for r in self.regions():
            if r.start > USER_ADDR_MAX or r.path in skip:
                continue
            if "r" not in r.perms:
                continue
            if data_only and "w" not in r.perms:
                continue
            out.append(r)
        return out

    def pattern_scan(
        self,
        pattern: bytes,
        *,
        data_only: bool = False,
        return_multiple: bool = True,
        limit: int | None = None,
        regions: list[MapRegion] | None = None,
        _chunk: int = 16 << 20,
    ) -> list[int] | int | None:
        """Search the target's memory for a byte-regex ``pattern`` (matched with re.DOTALL).

        Mirrors pymem's ``pattern_scan_all``: returns a list of absolute match addresses when
        ``return_multiple`` else the first address (or None). Large regions are read in chunks
        with overlap so matches spanning a chunk boundary aren't missed.

        When ``regions`` is given, scan EXACTLY those regions instead of the full
        ``scannable_regions(data_only=...)`` sweep. This is the cheap "warm-region" path used by
        the name scanner: most ticks only a handful of small regions actually contain names, so
        rescanning just those avoids re-reading hundreds of MB of heap every tick. ``data_only`` is
        ignored when ``regions`` is supplied (the caller already chose the region set). When
        ``regions`` is None the behavior is unchanged from before — existing callers are unaffected.
        """
        rx = re.compile(pattern, re.DOTALL)
        overlap = max(len(pattern), 1024)
        found: list[int] = []
        seen: set[int] = set()  # dedup matches that land in the chunk-overlap zone
        scan_regions = regions if regions is not None else self.scannable_regions(data_only=data_only)
        for region in scan_regions:
            pos = region.start
            while pos < region.end:
                size = min(_chunk, region.end - pos)
                try:
                    buf = self.read(pos, size)
                except OSError:
                    break  # region became unreadable; move on
                if not buf:
                    break
                for m in rx.finditer(buf):
                    addr = pos + m.start()
                    if addr in seen:
                        continue
                    if not return_multiple:
                        return addr
                    seen.add(addr)
                    found.append(addr)
                    if limit and len(found) >= limit:
                        return found
                # A short read (len < size) means we hit unmapped memory mid-region — stop here.
                if len(buf) < size:
                    break
                pos += _chunk - overlap  # step back by overlap to catch boundary matches
        if not return_multiple:
            return None
        return found
