"""Game files looked up the way the game does, from folders the API can read: the first source that has a file wins.

1. the haks, in the module's hak list order (the first one listed wins);
2. the module file itself;
3. an override folder;
4. the install's own `ovr` folders (lang/<language>/data/ovr, then ovr);
5. the base game's key and bif files (nwn_retail before nwn_base, each from the language folder first), as neverwinter.nim's resman does.

Only the file lists are read up front; a file's bytes are read when it is asked for. `refresh` reads the lists again when a source changed
(name, size, modified time), so new haks are seen without a restart.
"""

from __future__ import annotations

import hashlib
import logging
import struct
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# The resource types the API reads (neverwinter.nim restype.nim).
RESTYPES = {3: "tga", 2014: "ifo", 2015: "bic", 2017: "2da", 2018: "tlk", 2025: "uti", 2033: "dds"}
RESTYPE_IDS = {v: k for k, v in RESTYPES.items()}
KEYS = ("nwn_retail_loc", "nwn_retail", "nwn_base_loc", "nwn_base")


@dataclass(frozen=True)
class Prov:
    """Where one file is: a file on disk, and the part of it (offset, size; size -1 for the whole file); `source` names it."""
    path: Path
    offset: int
    size: int
    source: str


def erf_entries(path: Path) -> list[tuple[str, int, int, int]]:
    """[(resref, type, offset, size)] of an ERF V1.0/V1.1 (hak, mod, erf), without reading the data."""
    with path.open("rb") as f:
        h = f.read(160)
        if h[4:8] not in (b"V1.0", b"V1.1"):
            raise ValueError(f"{path.name}: not an ERF V1.0/V1.1")
        _lang_n, _lang_sz, n, _off_loc, off_keys, off_res = struct.unpack_from("<6I", h, 8)
        rlen = 32 if h[4:8] == b"V1.1" else 16
        ksz = rlen + 8
        f.seek(off_keys)
        keys = f.read(n * ksz)
        f.seek(off_res)
        res = f.read(n * 8)
    out = []
    for i in range(n):
        k = keys[i * ksz:(i + 1) * ksz]
        if len(k) < ksz:
            break
        rid, rtype = struct.unpack_from("<IH", k, rlen)
        off, size = struct.unpack_from("<II", res, rid * 8 if rid < n else i * 8)
        out.append((k[:rlen].split(b"\0", 1)[0].lower().decode("latin-1"), rtype, off, size))
    return out


def key_entries(key: Path, root: Path, lang_root: Path) -> list[tuple[str, int, Path, int, int]]:
    """[(resref, type, bif, offset, size)] of every file a KEY V1 lists. A bif is taken from the language's data folder when it is there,
    else from the install root (its name in the key is relative to that)."""
    data = key.read_bytes()
    if data[:8] != b"KEY V1  ":
        return []
    nbif, nkey, foff, koff = struct.unpack_from("<IIII", data, 8)
    bifs = []
    for i in range(nbif):
        _size, noff, nsize, _drives = struct.unpack_from("<IIHH", data, foff + i * 12)
        name = data[noff:noff + nsize].split(b"\0")[0].decode("latin-1").replace("\\", "/")
        local = lang_root / "data" / Path(name).name
        bifs.append(local if local.is_file() else root / name)
    wanted: dict[int, list[tuple[str, int, int]]] = {}
    for i in range(nkey):
        o = koff + i * 22
        name = data[o:o + 16].split(b"\0")[0].decode("latin-1").lower()
        typ, rid = struct.unpack_from("<HI", data, o + 16)
        wanted.setdefault(rid >> 20, []).append((name, typ, rid & 0xFFFFF))
    out = []
    for b, items in wanted.items():
        if b >= len(bifs) or not bifs[b].is_file():
            continue
        with bifs[b].open("rb") as f:
            head = f.read(20)
            if head[:8] != b"BIFFV1  ":
                continue
            nvar, _nfix, voff = struct.unpack_from("<III", head, 8)
            f.seek(voff)
            table = f.read(nvar * 16)
        for name, typ, idx in items:
            if idx < nvar:
                _id, off, size, _t = struct.unpack_from("<IIII", table, idx * 16)
                out.append((name, typ, bifs[b], off, size))
    return out


def _dir_entries(d: Path) -> list[tuple[str, int, Path]]:
    out = []
    for p in d.iterdir():
        stem, _, ext = p.name.lower().rpartition(".")
        if stem and ext in RESTYPE_IDS and p.is_file():
            out.append((stem, RESTYPE_IDS[ext], p))
    return out


class Resources:
    """The files the game would load for one module. `haks` are names without `.hak`, in hak list order."""

    def __init__(self, nwn_root: Optional[Path], language: str = "en", hak_dir: Optional[Path] = None, haks: tuple[str, ...] = (),
                 module: Optional[Path] = None, override_dir: Optional[Path] = None):
        self.nwn_root, self.language = nwn_root, language
        self.hak_dir, self.haks, self.module, self.override_dir = hak_dir, tuple(haks), module, override_dir
        self.sig: Optional[str] = None
        self.index: dict[tuple[str, int], Prov] = {}
        self._lock = threading.Lock()

    @property
    def lang_root(self) -> Optional[Path]:
        return self.nwn_root / "lang" / self.language if self.nwn_root else None

    def _sources(self) -> list[tuple[str, str, Path]]:
        """(kind, name, path), highest priority first; only those that exist."""
        out: list[tuple[str, str, Path]] = []
        if self.hak_dir is not None:
            out += [("erf", h, self.hak_dir / f"{h}.hak") for h in self.haks]
        if self.module is not None:
            out.append(("erf", "module", self.module))
        if self.override_dir is not None:
            out.append(("dir", "override", self.override_dir))
        if self.nwn_root is not None:
            out += [("dir", "ovr", self.lang_root / "data" / "ovr"), ("dir", "ovr", self.nwn_root / "ovr")]
            for k in KEYS:
                fn = self.lang_root / "data" / f"{k}.key"
                out.append(("key", "base", fn if fn.is_file() else self.nwn_root / "data" / f"{k}.key"))
        return [s for s in out if s[2].exists()]

    def refresh(self) -> bool:
        """Read the file lists again if a source changed. True when it did."""
        with self._lock:
            sources = self._sources()
            parts = []
            for _kind, _name, p in sources:
                try:
                    st = p.stat()
                    parts.append(f"{p}:{st.st_size}:{st.st_mtime_ns}")
                except OSError:
                    pass
            sig = hashlib.sha1("|".join(parts).encode()).hexdigest()
            if sig == self.sig:
                return False
            index: dict[tuple[str, int], Prov] = {}
            for kind, name, p in sources:
                try:
                    if kind == "erf":
                        for resref, typ, off, size in erf_entries(p):
                            index.setdefault((resref, typ), Prov(p, off, size, name))
                    elif kind == "dir":
                        for resref, typ, f in _dir_entries(p):
                            index.setdefault((resref, typ), Prov(f, 0, -1, name))
                    else:
                        for resref, typ, bif, off, size in key_entries(p, self.nwn_root, self.lang_root):
                            index.setdefault((resref, typ), Prov(bif, off, size, name))
                except (OSError, ValueError, struct.error):
                    logger.warning("could not read the file list of %s", p.name, exc_info=True)
            self.index, self.sig = index, sig
            return True

    def find(self, resref: str, ext: str) -> Optional[Prov]:
        return self.index.get((resref.lower(), RESTYPE_IDS[ext]))

    @staticmethod
    def read_prov(prov: Prov) -> bytes:
        with prov.path.open("rb") as f:
            if prov.size < 0:
                return f.read()
            f.seek(prov.offset)
            return f.read(prov.size)

    def read(self, resref: str, ext: str) -> Optional[bytes]:
        prov = self.find(resref, ext)
        return self.read_prov(prov) if prov else None

    def names(self, ext: str) -> list[str]:
        t = RESTYPE_IDS[ext]
        return sorted(r for r, typ in self.index if typ == t)
