"""The game's data as one object: the resources (resman.Resources), the talk tables and the 2DAs, read once and read again when the files
change. Built from the GAME_DATA_* settings; nothing is read until the first request.

The hak list and the custom talk table come from GAME_HAKS and GAME_CUSTOM_TLK, or, unset, from the module file's module.ifo (Mod_HakList,
Mod_CustomTlk) when GAME_MODULE_FILE is set.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Optional

from app.gamedata import gff
from app.gamedata.gff import _list, _v
from app.gamedata.item_value import Tables
from app.gamedata.resman import Resources, erf_entries
from app.gamedata.tables import CUSTOM_TLK_OFFSET, TwoDA, parse_tlk

logger = logging.getLogger(__name__)

# How often (seconds) a request may look at the files' modified times to see whether they changed.
CHECK_EVERY = 10.0


def module_ifo(module: Path) -> dict:
    """The module.ifo of a .mod file, as nwn_gff JSON."""
    for resref, typ, off, size in erf_entries(module):
        if resref == "module" and typ == 2014:
            with module.open("rb") as f:
                f.seek(off)
                return gff.read(f.read(size))
    raise ValueError(f"{module.name} has no module.ifo")


class GameData:
    def __init__(self, settings):
        s = settings
        self.nwn_root = Path(s.game_nwn_root) if s.game_nwn_root else None
        self.language = s.game_language
        self.hak_dir = Path(s.game_hak_dir) if s.game_hak_dir else None
        self.tlk_dir = Path(s.game_tlk_dir) if s.game_tlk_dir else self.hak_dir
        self.module = Path(s.game_module_file) if s.game_module_file else None
        self.override_dir = Path(s.game_override_dir) if s.game_override_dir else None
        self._haks_setting = tuple(h.strip() for h in s.game_haks.split(",") if h.strip())
        self._tlk_setting = s.game_custom_tlk.strip().removesuffix(".tlk")
        self.ilr = s.game_item_level_restriction
        self._lock = threading.RLock()
        self._checked = 0.0
        self._sig: Optional[tuple] = None
        self.res: Optional[Resources] = None
        self.dialog: list[str] = []
        self.custom: list[str] = []
        self.custom_tlk = ""
        self._2da: dict[str, Optional[TwoDA]] = {}
        self.tables = Tables(self._table_rows)

    # --- loading ------------------------------------------------------------------------------------------------------------------------

    def _module_sig(self) -> tuple:
        try:
            st = self.module.stat() if self.module else None
            return (st.st_size, st.st_mtime_ns) if st else ()
        except OSError:
            return ()

    def _tlk_paths(self) -> tuple[Optional[Path], Optional[Path]]:
        dialog = None
        if self.nwn_root:
            for p in (self.nwn_root / "lang" / self.language / "data" / "dialog.tlk", self.nwn_root / "dialog.tlk"):
                if p.is_file():
                    dialog = p
                    break
        custom = self.tlk_dir / f"{self.custom_tlk}.tlk" if (self.custom_tlk and self.tlk_dir) else None
        return dialog, custom

    @staticmethod
    def _stamp(p: Optional[Path]) -> tuple:
        try:
            st = p.stat() if p else None
            return (str(p), st.st_size, st.st_mtime_ns) if st else ()
        except OSError:
            return ()

    def ensure(self) -> None:
        """Load the data on first use, and again when a file changed (looked at no more often than CHECK_EVERY)."""
        now = time.monotonic()
        if self.res is not None and now - self._checked < CHECK_EVERY:
            return
        with self._lock:
            if self.res is not None and now - self._checked < CHECK_EVERY:
                return
            self._checked = now
            haks, tlk = self._haks_setting, self._tlk_setting
            mod_sig = self._module_sig()
            if self.module is not None and (not haks or not tlk) and mod_sig:
                try:
                    ifo = module_ifo(self.module)
                    haks = haks or tuple(str(_v(h.get("Mod_Hak"))) for h in _list(ifo.get("Mod_HakList")) if _v(h.get("Mod_Hak")))
                    tlk = tlk or str(_v(ifo.get("Mod_CustomTlk")) or "").strip()
                except (OSError, ValueError):
                    logger.warning("could not read module.ifo from %s", self.module.name, exc_info=True)
            self.custom_tlk = tlk
            if self.res is None or self.res.haks != haks:
                self.res = Resources(self.nwn_root, self.language, self.hak_dir, haks, self.module, self.override_dir)
            changed = self.res.refresh()
            dialog_p, custom_p = self._tlk_paths()
            sig = (self.res.sig, self._stamp(dialog_p), self._stamp(custom_p), mod_sig)
            if sig == self._sig and not changed:
                return
            self.dialog = self._load_tlk(dialog_p)
            self.custom = self._load_tlk(custom_p)
            self._2da.clear()
            self.tables = Tables(self._table_rows)
            self._sig = sig
            logger.info("game data loaded: %d resources, %d haks, %d dialog.tlk rows, %d custom TLK rows",
                        len(self.res.index), len(haks), len(self.dialog), len(self.custom))

    @staticmethod
    def _load_tlk(p: Optional[Path]) -> list[str]:
        if p is None or not p.is_file():
            if p is not None:
                logger.warning("talk table %s not found", p.name)
            return []
        try:
            return parse_tlk(p.read_bytes())
        except (OSError, ValueError):
            logger.warning("could not read the talk table %s", p.name, exc_info=True)
            return []

    # --- lookups ------------------------------------------------------------------------------------------------------------------------

    def strref(self, ref) -> str:
        """The text of a string reference ('' when there is none)."""
        try:
            n = int(ref)
        except (TypeError, ValueError):
            return ""
        if n >= CUSTOM_TLK_OFFSET:
            i = n - CUSTOM_TLK_OFFSET
            return self.custom[i] if 0 <= i < len(self.custom) else ""
        return self.dialog[n] if 0 <= n < len(self.dialog) else ""

    def twoda(self, name: str) -> Optional[TwoDA]:
        key = name.lower().removesuffix(".2da")
        with self._lock:
            if key not in self._2da:
                data = self.res.read(key, "2da") if self.res else None
                try:
                    self._2da[key] = TwoDA.parse(data) if data is not None else None
                except ValueError:
                    logger.warning("%s.2da is not a 2DA", key)
                    self._2da[key] = None
            return self._2da[key]

    def _table_rows(self, name: str) -> Optional[dict]:
        t = self.twoda(name)
        return t.as_dict() if t is not None else None

    def source(self, resref: str, ext: str) -> Optional[str]:
        prov = self.res.find(resref, ext) if self.res else None
        return prov.source if prov else None

    def blueprint(self, resref: str) -> Optional[dict]:
        data = self.res.read(resref, "uti") if self.res else None
        return gff.read(data) if data is not None else None

    def max_single(self) -> list[int]:
        """itemvalue.2da's MAXSINGLEITEMVALUE by level (the item level restriction)."""
        t = self.twoda("itemvalue")
        out = []
        for i in range(t.row_count if t else 0):
            v = t.get_int(i, "MAXSINGLEITEMVALUE")
            out.append(v if v is not None else 0)
        return out

    def named(self, table: str, row: int, name_col: str = "Name", label_col: str = "Label") -> str:
        """A 2DA row's name from the talk table, else its plain-text label; '' when the row is not there."""
        t = self.twoda(table)
        if t is None:
            return ""
        text = self.strref(t.get_int(row, name_col))
        return text or (t.get(row, label_col) or "")
