"""A GFF V3.2 reader (character files, item blueprints, module.ifo) giving neverwinter.nim's nwn_gff JSON shape: {"type", "value"} per field,
lists of {"__struct_id", ...}, locstrings {"id", "<language>"}. Pure Python, so the API needs no nwn_gff binary to read a file.
"""
from __future__ import annotations

import base64
import struct

TYPES = ("byte", "char", "word", "short", "dword", "int", "dword64", "int64", "float", "double", "cexostring", "resref", "cexolocstring",
         "void", "struct", "list")


def read(data: bytes) -> dict:
    """The GFF as nwn_gff's JSON (the top-level struct with __data_type). ValueError for anything that is not GFF V3.2."""
    if len(data) < 56 or data[4:8] != b"V3.2":
        raise ValueError("not a GFF V3.2 file")
    (s_off, s_n, f_off, f_n, l_off, l_n, d_off, _d_n, fi_off, _fi_n, li_off, _li_n) = struct.unpack_from("<12I", data, 8)

    def label(i: int) -> str:
        return data[l_off + i * 16:l_off + i * 16 + 16].split(b"\0")[0].decode("latin-1")

    def text(b: bytes) -> str:
        return b.decode("utf-8") if b.isascii() else b.decode("cp1252", errors="replace")

    def field(i: int, depth: int):
        typ, lab, raw = struct.unpack_from("<3I", data, f_off + i * 12)
        name = TYPES[typ] if typ < len(TYPES) else "void"
        at = d_off + raw
        if typ in (0, 2, 4):
            v = raw & (0xFF if typ == 0 else 0xFFFF if typ == 2 else 0xFFFFFFFF)
        elif typ == 1:
            v = struct.unpack("<b", struct.pack("<B", raw & 0xFF))[0]
        elif typ == 3:
            v = struct.unpack("<h", struct.pack("<H", raw & 0xFFFF))[0]
        elif typ == 5:
            v = struct.unpack("<i", struct.pack("<I", raw))[0]
        elif typ == 6:
            v = struct.unpack_from("<Q", data, at)[0]
        elif typ == 7:
            v = struct.unpack_from("<q", data, at)[0]
        elif typ == 8:
            v = struct.unpack("<f", struct.pack("<I", raw))[0]
        elif typ == 9:
            v = struct.unpack_from("<d", data, at)[0]
        elif typ == 10:
            n = struct.unpack_from("<I", data, at)[0]
            v = text(data[at + 4:at + 4 + n])
        elif typ == 11:
            n = data[at]
            v = text(data[at + 1:at + 1 + n])
        elif typ == 12:
            _size, ref, count = struct.unpack_from("<3I", data, at)
            v, p = {}, at + 12
            if ref != 0xFFFFFFFF:
                v["id"] = ref
            for _ in range(count):
                sid, n = struct.unpack_from("<iI", data, p)
                v[str(sid)] = text(data[p + 8:p + 8 + n])
                p += 8 + n
        elif typ == 13:
            n = struct.unpack_from("<I", data, at)[0]
            v = base64.b64encode(data[at + 4:at + 4 + n]).decode()
        elif typ == 14:
            v = struct_(raw, depth + 1)
        elif typ == 15:
            p = li_off + raw
            n = struct.unpack_from("<I", data, p)[0]
            v = [struct_(x, depth + 1) for x in struct.unpack_from(f"<{n}I", data, p + 4)]
        else:
            v = None
        if typ == 13:   # nwn_gff gives binary data as base64 under value64
            return label(lab), {"type": name, "value64": v}
        if typ == 14:   # nwn_gff repeats a struct field's id beside its type
            return label(lab), {"__struct_id": v.get("__struct_id", 0), "type": name, "value": v}
        return label(lab), {"type": name, "value": v}

    def struct_(i: int, depth: int) -> dict:
        if depth > 32 or i >= s_n:
            raise ValueError("GFF nesting too deep or a bad struct index")
        sid, raw, n = struct.unpack_from("<3I", data, s_off + i * 12)
        out: dict = {} if i == 0 else {"__struct_id": sid}
        idx = [raw] if n == 1 else list(struct.unpack_from(f"<{n}I", data, fi_off + raw)) if n else []
        for fi in idx:
            if fi < f_n:
                k, v = field(fi, depth)
                out[k] = v
        return out

    top = {"__data_type": data[:4].decode("latin-1")}
    top.update(struct_(0, 0))
    return top


def _v(node):
    """A field's value: nwn_gff wraps every one as {"type", "value"}."""
    if isinstance(node, dict) and "type" in node and "value" in node:
        return node["value"]
    return node


def _locstring(node) -> str:
    """The first non-empty text of a localized string (its languages in order); '' when it has only a TLK reference."""
    inner = _v(node)
    if isinstance(inner, dict):
        for k in sorted(inner.keys(), key=lambda x: int(x) if str(x).isdigit() else 999):
            if k != "id" and inner[k]:
                return inner[k]
    elif isinstance(inner, str):
        return inner
    return ""


def _list(node) -> list[dict]:
    inner = _v(node)
    return inner if isinstance(inner, list) else []
