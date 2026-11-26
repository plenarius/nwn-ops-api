"""The game's talk tables (TLK V3.0) and 2DA tables, read from bytes.

- TLK: a header, then 40-byte records (flags, sound, volume, pitch, offset, size, length), then the strings.
- 2DA V2.0: the version line, an optional DEFAULT line, the column names, then one row per line, its index first. Cells are separated by
  white space and grouped by double quotes; `****` is an empty cell. The engine reads rows by position, not by the index written in them.
- A string reference at or above CUSTOM_TLK_OFFSET is a row of the module's custom talk table; below it, of dialog.tlk.
"""

from __future__ import annotations

import re
import struct
from typing import Optional

CUSTOM_TLK_OFFSET = 0x01000000  # 16777216


def parse_tlk(data: bytes) -> list[str]:
    """The strings of a TLK file indexed by string reference ('' for an empty row). ValueError if it is not a TLK."""
    if len(data) < 20 or data[:4] != b"TLK ":
        raise ValueError("not a TLK file")
    string_count, entries_offset = struct.unpack_from("<II", data, 12)
    out: list[str] = []
    for i in range(string_count):
        record = 20 + i * 40
        if record + 40 > len(data):
            break
        offset, size = struct.unpack_from("<II", data, record + 28)
        if size == 0:
            out.append("")
            continue
        raw = data[entries_offset + offset:entries_offset + offset + size]
        # EE writes UTF-8; older tables are Windows-1252.
        try:
            out.append(raw.decode("utf-8"))
        except UnicodeDecodeError:
            out.append(raw.decode("cp1252", errors="replace"))
    return out


_CELL = re.compile(r'"([^"]*)"|(\S+)')


def parse_2da_text(text: str) -> tuple[list[str], list[list[str]]]:
    """(column names, rows): rows[i] lines up with the columns, empty cells ('****', or missing at the end of a row) as ''. ValueError if
    it is not a 2DA."""
    lines = [ln.rstrip("\r") for ln in text.split("\n")]
    if not lines or not lines[0].lstrip("﻿").startswith("2DA"):
        raise ValueError("not a 2DA file")
    header = None
    for i, ln in enumerate(lines[1:], start=1):
        s = ln.strip()
        if not s or s.upper().startswith("DEFAULT:"):
            continue
        header = i
        break
    if header is None:
        return [], []
    columns = lines[header].split()
    rows: list[list[str]] = []
    for ln in lines[header + 1:]:
        if not ln.strip():
            continue
        cells = [m.group(1) if m.group(1) is not None else m.group(2) for m in _CELL.finditer(ln)]
        if len(cells) < 2:
            continue
        body = ["" if c == "****" else c for c in cells[1:len(columns) + 1]]
        body += [""] * (len(columns) - len(body))
        rows.append(body)
    return columns, rows


class TwoDA:
    def __init__(self, columns: list[str], rows: list[list[str]]):
        self.columns = columns
        self.rows = rows
        self._col = {c.lower(): i for i, c in enumerate(columns)}

    @classmethod
    def parse(cls, data: bytes) -> "TwoDA":
        return cls(*parse_2da_text(data.decode("cp1252", errors="replace")))

    def get(self, row: int, col: str) -> Optional[str]:
        """A cell, the column found whatever its case; None for an empty cell or one that is not there."""
        if row < 0 or row >= len(self.rows):
            return None
        idx = self._col.get(col.lower())
        if idx is None:
            return None
        v = self.rows[row][idx]
        return v if v else None

    def get_int(self, row: int, col: str) -> Optional[int]:
        v = self.get(row, col)
        try:
            return int(v, 0) if v is not None else None
        except ValueError:
            return None

    def row(self, row: int) -> Optional[dict[str, Optional[str]]]:
        """{column: cell or None} of one row, the columns in the file's order; None past the last row."""
        if row < 0 or row >= len(self.rows):
            return None
        return {c: (self.rows[row][i] or None) for i, c in enumerate(self.columns)}

    def as_dict(self) -> dict[int, dict[str, str]]:
        """{row: {column: text}}, the shape item_value.Tables reads."""
        return {i: dict(zip(self.columns, r)) for i, r in enumerate(self.rows)}

    @property
    def row_count(self) -> int:
        return len(self.rows)
