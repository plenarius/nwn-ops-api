"""Writers for the game's file formats, for the tests only: GFF V3.2 (from nwn_gff's JSON shape), ERF V1.0, KEY V1 with its BIF, TLK V3.0,
and a small made-up game install built from them. Every name, string and number here is invented."""

from __future__ import annotations

import base64
import struct
from pathlib import Path

TYPES = ("byte", "char", "word", "short", "dword", "int", "dword64", "int64", "float", "double", "cexostring", "resref", "cexolocstring",
         "void", "struct", "list")


def write_gff(doc: dict) -> bytes:
    """GFF V3.2 bytes of a document in nwn_gff's JSON shape (the top struct with __data_type)."""
    structs: list[list] = []      # [id, field indices]
    fields: list[tuple] = []      # (type, label index, data or offset)
    labels: list[str] = []
    data = bytearray()
    lists = bytearray()

    def label(name: str) -> int:
        if name not in labels:
            labels.append(name)
        return labels.index(name)

    def add_struct(sid: int, body: dict) -> int:
        i = len(structs)
        structs.append([sid, []])
        idx = []
        for k, node in body.items():
            if k.startswith("__"):
                continue
            idx.append(add_field(k, node))
        structs[i][1] = idx
        return i

    def add_field(name: str, node: dict) -> int:
        typ = TYPES.index(node["type"])
        v = node.get("value", node.get("value64"))
        fi = len(fields)
        fields.append(None)
        if typ in (0, 2, 4):
            raw = int(v)
        elif typ == 1:
            raw = struct.unpack("<B", struct.pack("<b", v))[0]
        elif typ == 3:
            raw = struct.unpack("<H", struct.pack("<h", v))[0]
        elif typ == 5:
            raw = struct.unpack("<I", struct.pack("<i", v))[0]
        elif typ == 8:
            raw = struct.unpack("<I", struct.pack("<f", v))[0]
        elif typ in (6, 7, 9, 10, 11, 12, 13):
            raw = len(data)
            if typ == 6:
                data.extend(struct.pack("<Q", v))
            elif typ == 7:
                data.extend(struct.pack("<q", v))
            elif typ == 9:
                data.extend(struct.pack("<d", v))
            elif typ == 10:
                b = v.encode("utf-8")
                data.extend(struct.pack("<I", len(b)) + b)
            elif typ == 11:
                b = v.encode("utf-8")
                data.extend(bytes([len(b)]) + b)
            elif typ == 12:
                subs = [(int(k), s.encode("utf-8")) for k, s in v.items() if k != "id"]
                body = b"".join(struct.pack("<iI", k, len(s)) + s for k, s in subs)
                data.extend(struct.pack("<3I", 8 + len(body), v.get("id", 0xFFFFFFFF), len(subs)) + body)
            else:
                b = base64.b64decode(v)
                data.extend(struct.pack("<I", len(b)) + b)
        elif typ == 14:
            raw = add_struct(v.get("__struct_id", 0), v)
        else:
            idx = [add_struct(s.get("__struct_id", 0), s) for s in v]
            raw = len(lists)
            lists.extend(struct.pack("<I", len(idx)) + b"".join(struct.pack("<I", x) for x in idx))
        fields[fi] = (typ, label(name), raw)
        return fi

    add_struct(0xFFFFFFFF, doc)
    field_idx = bytearray()
    sbytes = bytearray()
    for sid, idx in structs:
        if len(idx) == 1:
            sbytes.extend(struct.pack("<3I", sid, idx[0], 1))
        else:
            sbytes.extend(struct.pack("<3I", sid, len(field_idx), len(idx)))
            field_idx.extend(b"".join(struct.pack("<I", x) for x in idx))
    fbytes = b"".join(struct.pack("<3I", *f) for f in fields)
    lbytes = b"".join(n.encode().ljust(16, b"\0") for n in labels)
    off = 56
    parts = [(sbytes, len(structs)), (fbytes, len(fields)), (lbytes, len(labels)), (bytes(data), len(data)),
             (bytes(field_idx), len(field_idx)), (bytes(lists), len(lists))]
    header = bytearray(doc["__data_type"].encode().ljust(4)[:4] + b"V3.2")
    for blob, count in parts:
        header.extend(struct.pack("<II", off, count))
        off += len(blob)
    return bytes(header) + b"".join(blob for blob, _ in parts)


RESTYPE = {"2da": 2017, "uti": 2025, "ifo": 2014, "bic": 2015, "tga": 3}


def write_erf(files: dict[str, bytes], kind: bytes = b"HAK ") -> bytes:
    """ERF V1.0 of {"name.ext": bytes}."""
    n = len(files)
    off_keys = 160
    off_res = off_keys + n * 24
    off_data = off_res + n * 8
    keys, res, blob = bytearray(), bytearray(), bytearray()
    for i, (name, data) in enumerate(files.items()):
        stem, ext = name.rsplit(".", 1)
        keys.extend(stem.encode().ljust(16, b"\0") + struct.pack("<IHH", i, RESTYPE[ext], 0))
        res.extend(struct.pack("<II", off_data + len(blob), len(data)))
        blob.extend(data)
    head = kind + b"V1.0" + struct.pack("<6I", 0, 0, n, 160, off_keys, off_res)
    return head.ljust(160, b"\0") + bytes(keys) + bytes(res) + bytes(blob)


def write_key_bif(root: Path, key_name: str, bif_rel: str, files: dict[str, bytes], key_dir: Path | None = None) -> None:
    """A KEY V1 (in root/data, or key_dir) listing one BIF (at root/bif_rel) holding the files."""
    n = len(files)
    table, blob = bytearray(), bytearray()
    voff = 20
    doff = voff + n * 16
    for i, (name, data) in enumerate(files.items()):
        table.extend(struct.pack("<IIII", i, doff + len(blob), len(data), RESTYPE[name.rsplit(".", 1)[1]]))
        blob.extend(data)
    bif = b"BIFFV1  " + struct.pack("<III", n, 0, voff) + bytes(table) + bytes(blob)
    (root / bif_rel).parent.mkdir(parents=True, exist_ok=True)
    (root / bif_rel).write_bytes(bif)
    name_b = bif_rel.encode() + b"\0"
    foff = 64
    noff = foff + 12
    koff = noff + len(name_b)
    keys = bytearray()
    for i, name in enumerate(files):
        stem, ext = name.rsplit(".", 1)
        keys.extend(stem.encode().ljust(16, b"\0") + struct.pack("<HI", RESTYPE[ext], (0 << 20) | i))
    head = b"KEY V1  " + struct.pack("<IIII", 1, n, foff, koff)
    key = head.ljust(64, b"\0") + struct.pack("<IIHH", len(bif), noff, len(name_b), 0) + name_b + bytes(keys)
    d = key_dir or root / "data"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{key_name}.key").write_bytes(key)


def write_tlk(strings: list[str]) -> bytes:
    n = len(strings)
    entries = 20 + n * 40
    recs, blob = bytearray(), bytearray()
    for s in strings:
        b = s.encode("utf-8")
        recs.extend(struct.pack("<I16sIIIIf", 1 if b else 0, b"", 0, 0, len(blob), len(b), 0.0))
        blob.extend(b)
    return b"TLK V3.0" + struct.pack("<III", 0, n, entries) + bytes(recs) + bytes(blob)


def twoda(columns: list[str], rows: list[list[str]]) -> bytes:
    lines = ["2DA V2.0", "", "   " + " ".join(columns)]
    lines += [f"{i} " + " ".join(r) for i, r in enumerate(rows)]
    return ("\n".join(lines) + "\n").encode()


# --- a made-up game -----------------------------------------------------------------------------------------------------------------------

def N(v, t="int"):
    return {"type": t, "value": v}


def loc(text=None, ref=None):
    v = {}
    if ref is not None:
        v["id"] = ref
    if text is not None:
        v["0"] = text
    return {"type": "cexolocstring", "value": v}


DIALOG = ["", "Fighter", "Wizard", "Human", "Elf", "Spot", "Lore", "Alertness", "Toughness", "Longsword", "Ring", "Enhancement Bonus:",
          "+1", "+2", "Weight:", "GP:", "Value", "Base Item:", "Special Properties:", "Level Required:", "(Unidentified)", "Ability Bonus:",
          "Strength", "A plain blade.", "Charges:", "Temporary", "Unknown"]
S = {s: i for i, s in enumerate(DIALOG)}
CUSTOM = ["Rune Knight", "Rune Sight"]


def base_tables() -> dict[str, bytes]:
    ip = lambda *a: list(a)
    return {
        "classes.2da": twoda(["Label", "Name", "StatGainTable"], [["Fighter", str(S["Fighter"]), "****"], ["Wizard", str(S["Wizard"]), "****"],
                                                                 ["RuneKnight", "16777216", "cls_stat_rune"]]),
        "cls_stat_rune.2da": twoda(["Level", "Str", "Dex", "Con", "Wis", "Int", "Cha"], [["1", "1", "0", "0", "0", "0", "0"],
                                                                                         ["2", "0", "0", "1", "0", "0", "0"]]),
        "racialtypes.2da": twoda(["Label", "Name", "StrAdjust", "DexAdjust", "ConAdjust", "IntAdjust", "WisAdjust", "ChaAdjust"],
                                 [["Human", str(S["Human"]), "0", "0", "0", "0", "0", "0"], ["Elf", str(S["Elf"]), "0", "2", "-2", "0", "0", "0"]]),
        "skills.2da": twoda(["Label", "Name", "KeyAbility", "Untrained"], [["Spot", str(S["Spot"]), "WIS", "1"], ["Lore", str(S["Lore"]), "INT", "1"]]),
        "feat.2da": twoda(["LABEL", "FEAT"], [["Alertness", str(S["Alertness"])], ["Toughness", str(S["Toughness"])], ["NoName", "****"]]),
        "portraits.2da": twoda(["BaseResRef", "Sex"], [["hu_m_01_", "0"], ["el_f_02_", "1"]]),
        "baseitems.2da": twoda(["label", "Name", "BaseItemStatRef", "Description", "TenthLBS", "BaseCost", "ItemMultiplier", "Stacking",
                                "Category", "ModelType", "Container", "AC_Enchant", "ILRStackSize"],
                               [["longsword", str(S["Longsword"]), "****", str(S["A plain blade."]), "40", "15", "1", "1", "1", "3", "0", "****", "1"],
                                ["ring", str(S["Ring"]), "****", "****", "1", "100", "1", "1", "6", "0", "0", "****", "1"]]),
        "itemvalue.2da": twoda(["LABEL", "MAXSINGLEITEMVALUE"], [["1", "1000"], ["2", "5000"], ["3", "20000"]]),
        "itempropdef.2da": twoda(["Name", "Label", "SubTypeResRef", "Cost", "CostTableResRef", "Param1ResRef", "GameStrRef"],
                                 [ip("****", "Ability", "IPRP_ABILITIES", "1", "1", "****", str(S["Ability Bonus:"])),
                                  ip("****", "Enhancement", "****", "1", "1", "****", str(S["Enhancement Bonus:"]))]),
        "iprp_abilities.2da": twoda(["Label", "Name", "Cost"], [["STR", str(S["Strength"]), "1"]]),
        "iprp_costtable.2da": twoda(["Name", "Label"], [["IPRP_BASE1", "Base"], ["IPRP_BONUSCOST", "Bonus"]]),
        "iprp_bonuscost.2da": twoda(["Name", "Cost", "Value"], [["****", "0", "0"], [str(S["+1"]), "1", "1"], [str(S["+2"]), "2", "2"]]),
        "ruleset.2da": twoda(["Label", "Value"], [["MAX_CHARGES_FOR_ITEM_COST", "50"]]),
    }


def sword(name="Rusty Blade", ident=1, plot=0, props=((1, 0, 1),)) -> dict:
    return {"__data_type": "UTI ", "TemplateResRef": N("tst_sword", "resref"), "Tag": N("TST_SWORD", "cexostring"),
            "LocalizedName": loc(name), "BaseItem": N(0), "Identified": N(ident, "byte"), "Plot": N(plot, "byte"),
            "StackSize": N(1, "word"), "Charges": N(0, "byte"), "AddCost": N(0, "dword"),
            "Description": loc(ref=S["A plain blade."]), "DescIdentified": loc("A blade with a story."),
            "PropertiesList": {"type": "list", "value": [
                {"__struct_id": 0, "PropertyName": N(p, "word"), "Subtype": N(sub, "word"), "CostTable": N(1, "byte"),
                 "CostValue": N(cv, "word"), "Param1": N(255, "byte"), "Param1Value": N(0, "byte")} for p, sub, cv in props]}}


def make_game(root: Path) -> dict[str, Path]:
    """A game install, a hak folder with one hak, a module and a custom talk table under `root`. Returns the paths."""
    nwn = root / "nwn"
    tables = base_tables()
    base_files = {**tables, "tst_sword.uti": write_gff(sword()), "tst_basering.uti": write_gff({**sword("Plain Ring", props=()),
                                                                                                "BaseItem": N(1)})}
    write_key_bif(nwn, "nwn_base", "data/base.bif", base_files)
    (nwn / "lang" / "en" / "data").mkdir(parents=True)
    (nwn / "lang" / "en" / "data" / "dialog.tlk").write_bytes(write_tlk(DIALOG))
    haks = root / "hak"
    haks.mkdir()
    # The hak changes feat.2da (a third feat gets a name) and has its own sword.
    feat = twoda(["LABEL", "FEAT"], [["Alertness", str(S["Alertness"])], ["Toughness", str(S["Toughness"])], ["RuneSight", "16777217"]])
    (haks / "tst_top.hak").write_bytes(write_erf({"feat.2da": feat, "tst_sword.uti": write_gff(sword("Hak Blade", props=((1, 0, 2),)))}))
    (haks / "tst_low.hak").write_bytes(write_erf({"feat.2da": tables["feat.2da"], "tst_lowonly.uti": write_gff(sword("Low Blade"))}))
    tlk = root / "tlk"
    tlk.mkdir()
    (tlk / "tst_custom.tlk").write_bytes(write_tlk(CUSTOM))
    ifo = {"__data_type": "IFO ", "Mod_CustomTlk": N("tst_custom", "cexostring"),
           "Mod_HakList": {"type": "list", "value": [{"__struct_id": 8, "Mod_Hak": N("tst_top", "cexostring")},
                                                     {"__struct_id": 8, "Mod_Hak": N("tst_low", "cexostring")}]}}
    module = root / "tst.mod"
    module.write_bytes(write_erf({"module.ifo": write_gff(ifo), "tst_modring.uti": write_gff({**sword("Module Ring", props=()), "BaseItem": N(1)})},
                                 kind=b"MOD "))
    return {"nwn": nwn, "hak": haks, "tlk": tlk, "module": module}


def character(**over) -> dict:
    """A made-up level 3 character (Fighter 1, Rune Knight 2) wearing a sword (+1 Strength) and a ring."""
    doc = {
        "__data_type": "BIC ", "FirstName": loc("Testa"), "LastName": loc("Example"), "Description": loc("Made up for the tests."),
        "Gender": N(1, "byte"), "Race": N(1, "byte"), "Subrace": N("", "cexostring"), "Deity": N("The Test", "cexostring"),
        "LawfulChaotic": N(80, "byte"), "GoodEvil": N(20, "byte"), "Age": N(30), "MaxHitPoints": N(24, "short"),
        "CurrentHitPoints": N(20, "short"), "Experience": N(3000, "dword"), "Portrait": N("", "resref"), "PortraitId": N(1, "word"),
        "Str": N(14, "byte"), "Dex": N(12, "byte"), "Con": N(13, "byte"), "Int": N(10, "byte"), "Wis": N(8, "byte"), "Cha": N(10, "byte"),
        "ClassList": {"type": "list", "value": [{"__struct_id": 2, "Class": N(0), "ClassLevel": N(1, "short")},
                                                {"__struct_id": 2, "Class": N(2), "ClassLevel": N(2, "short")}]},
        "FeatList": {"type": "list", "value": [{"__struct_id": 1, "Feat": N(1, "word")}, {"__struct_id": 1, "Feat": N(2, "word")}]},
        "SkillList": {"type": "list", "value": [{"__struct_id": 0, "Rank": N(4, "byte")}, {"__struct_id": 0, "Rank": N(0, "byte")}]},
        "Equip_ItemList": {"type": "list", "value": [
            {**{k: v for k, v in sword("Testa's Blade", props=((0, 0, 1),)).items() if k != "__data_type"}, "__struct_id": 0x10},
            {**{k: v for k, v in sword("Odd Ring", ident=0, props=((1, 0, 1),)).items() if k != "__data_type"}, "BaseItem": N(1),
             "__struct_id": 0x80},
            {"__struct_id": 0x20000, "BaseItem": N(1), "LocalizedName": loc("Hide")}]},
        "CombatInfo": {"__struct_id": 51882, "type": "struct", "value": {"__struct_id": 51882, "NumAttacks": N(1, "byte")}},
        "SQLite": {"__struct_id": 0, "type": "struct", "value": {"__struct_id": 0, "Data": {"type": "void", "value64": "AAEC"}}},
        "Lvl": {"type": "float", "value": 1.5}, "Big": {"type": "dword64", "value": 2**40}, "Neg": {"type": "int64", "value": -5},
        "Dbl": {"type": "double", "value": 0.25}, "Sh": {"type": "short", "value": -3}, "Ch": {"type": "char", "value": -1},
    }
    doc.update(over)
    return doc
