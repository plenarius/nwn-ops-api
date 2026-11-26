"""What the game shows when a player examines an item: its description, kind, weight, value and properties, worded as the client words them.

Ported from the game, not guessed:
- the client's CGuiExamineItem::SetDescriptionData (the lines and their order), ::AddProperty (one property's text) and ::SortProperties
  (properties listed in strcmp order);
- the server's SendServerToPlayerExamineGui_ItemData (which properties are sent, and only for an identified item), CNWSItem::GetWeight and
  ComputeWeight, CItemRepository::CalculateContentsWeight (nwmain and nwserver 8193.37).

Tables come from the same Tables as the item value code; a table the client cannot load drops that property, as it does.
"""

from __future__ import annotations

from typing import Callable, Optional

from app.gamedata import item_value
from app.gamedata.gff import _v
from app.gamedata.item_value import f32, trunc

# Strings from dialog.tlk the client's examine panel uses.
STR_WEIGHT, STR_GP, STR_VALUE, STR_BASE_ITEM, STR_PROPERTIES, STR_UNKNOWN, STR_CHARGES, STR_LEVEL, STR_TEMPORARY = (
    5567, 6407, 3048, 53302, 6806, 53294, 5565, 6807, 83389)
# What the client adds after an unidentified item's base item name: "(Unidentified)".
STR_UNIDENTIFIED = 5548


# The properties the server never sends (passive type 81, weight increase, and 83, visual effect) and the active ones (CNWSItem::ActiveProperty).
UNSENT = (0x51, 0x53)
ACTIVE = (0x0F, 0x37, 0x46, 0x50)

ARMOR_BASE_ITEM = 16
WEIGHT_REDUCTION, CONTAINER_REDUCED_WEIGHT, WEIGHT_INCREASE = 0x0B, 0x20, 0x51
REDCOST_TABLE = 15                       # iprp_costtable row of the container weight reduction table


def _cell(t: item_value.Tables, table: str, row: int, col: str) -> Optional[str]:
    """A cell of a 2DA, the column found whatever its case (the engine's lookups ignore case); None for '****' or missing."""
    tab = t.table(table)
    r = tab.get(row) if tab else None
    if not r:
        return None
    for k, v in r.items():
        if k.lower() == col.lower():
            return None if v in (None, "", "****") else v
    return None


def _int(t: item_value.Tables, table: str, row: int, col: str) -> Optional[int]:
    v = _cell(t, table, row, col)
    try:
        return int(v, 0) if v is not None else None
    except ValueError:
        return None


def _float(t: item_value.Tables, table: str, row: int, col: str) -> Optional[float]:
    v = _cell(t, table, row, col)
    try:
        return f32(float(v)) if v is not None else None
    except ValueError:
        return None


def _ref(t: item_value.Tables, table: str, row: int, col: str) -> int:
    """A strref column: -1 (no string) where the cell is missing, as the client's GetINTEntry default is."""
    v = _int(t, table, row, col)
    return -1 if v is None else v


def character_level(gff: dict) -> int:
    """CNWSCreatureStats::GetLevel(0): their total level."""
    stats = _v(gff.get("LvlStatList")) or []
    if stats:
        return len(stats)
    return sum(_v(c.get("ClassLevel", 0)) or 0 for c in (_v(gff.get("ClassList")) or []))


def _props(item: dict) -> list[dict]:
    return _v(item.get("PropertiesList")) or []


def _prop(p: dict) -> tuple[int, int, int, int, int]:
    """(type, subtype, cost table, cost value, param1 value) of one entry of PropertiesList."""
    g = lambda k: _v(p.get(k, 0)) or 0
    return g("PropertyName"), g("Subtype"), g("CostTable"), g("CostValue"), g("Param1Value")


def item_weight(item: dict, t: item_value.Tables, base: Optional[dict] = None) -> int:
    """CNWSItem::GetWeight, in tenths of a pound: its own weight (times the stack), or for a container its own plus its contents'."""
    own = _own_weight(item, t)
    contents = _v(item.get("ItemList"))
    if _is_container(item, t):
        return own + contents_weight(item, contents or [], t)
    n = _v(item.get("StackSize", 1)) or 1
    return own * n if n > 1 else own


def _is_container(item: dict, t: item_value.Tables) -> bool:
    return (_int(t, "baseitems", _v(item["BaseItem"]), "Container") or 0) != 0


def _own_weight(item: dict, t: item_value.Tables) -> int:
    """CNWSItem::ComputeWeight: the base weight (an armor's comes from armor.2da) plus weight increases, times the weight reductions."""
    base = _v(item["BaseItem"])
    if _int(t, "baseitems", base, "ModelType") == 3:
        w = _int(t, "armor", item_value.armor_ac(item, t), "WEIGHT") or 0
    else:
        w = _int(t, "baseitems", base, "TenthLBS") or 0
    mult, extra = 1.0, 0
    for p in _props(item):
        ptype, _sub, ctab, cval, p1val = _prop(p)
        if ptype in ACTIVE:
            continue
        if ptype == WEIGHT_REDUCTION:
            name = t.cost_table(ctab)
            v = _float(t, name, cval, "Value") if name else None
            if v is not None and not (mult <= v):
                mult = v
        elif ptype == WEIGHT_INCREASE:
            v = _int(t, "iprp_weightinc", p1val, "Value")
            if v is not None:
                extra += v
    return trunc(f32(f32(float(extra + w)) * mult))


def contents_weight(bag: dict, contents: list[dict], t: item_value.Tables) -> int:
    """CItemRepository::CalculateContentsWeight: each thing inside, less the bag's own weight reduction if it has one."""
    red = next((p for p in _props(bag) if _prop(p)[0] == CONTAINER_REDUCED_WEIGHT), None)
    total = 0
    for c in contents:
        w = item_weight(c, t)
        if red is not None:
            name = t.cost_table(REDCOST_TABLE)
            m = _float(t, name, _prop(red)[3], "Value") if name else None
            w = trunc(f32(float(w) - f32(float(w) * (m or 0.0))))
        total += w
    return total


def property_text(ptype: int, sub: int, cost_value: int, p1_value: int, base: int, t: item_value.Tables,
                  strref: Callable[[int], str], temporary: bool = False) -> Optional[str]:
    """CGuiExamineItem::AddProperty: the property type's name, then its subtype's, cost value's and parameter's, each after a space.
    None where the client adds nothing (no name for the type, or a table it cannot load)."""
    game_str = _int(t, "itempropdef", ptype, "GameStrRef")
    if game_str is None:
        return None
    subtable = _cell(t, "itempropdef", ptype, "SubTypeResRef")
    cost_idx = _int(t, "itempropdef", ptype, "CostTableResRef") or 0
    p1_idx = _int(t, "itempropdef", ptype, "Param1ResRef")
    has_p1 = p1_idx is not None
    if p1_idx is None:
        p1_idx = -1
    text = strref(game_str)
    if subtable:
        if t.table(subtable.lower()) is None:
            return None
        name = _int(t, subtable.lower(), sub, "Name")
        c = _int(t, subtable.lower(), sub, "CostTableResRef")
        if c:
            cost_idx = c
        p = _int(t, subtable.lower(), sub, "Param1ResRef")
        if p is not None:
            has_p1, p1_idx = True, p
        text = text + " " + strref(name if name is not None else -1)
    if cost_idx != 0:
        ctname = _cell(t, "iprp_costtable", cost_idx, "Name")
        if not ctname or t.table(ctname.lower()) is None:
            return None
        n = _int(t, ctname.lower(), cost_value, "Name")
        text = text + " " + strref(n if n is not None else -1)
    if has_p1:
        ptname = _cell(t, "iprp_paramtable", p1_idx, "TableResRef")
        if not ptname or t.table(ptname.lower()) is None:
            return None
        n = _int(t, ptname.lower(), p1_value, "Name")
        text = text + " " + strref(n if n is not None else -1)
    if 1 <= ptype <= 5:
        ac_type = _int(t, "baseitems", base, "AC_Enchant")
        n = _int(t, "iprp_acmodtype", ac_type, "Name") if ac_type is not None else None
        if n is not None:
            text = text + " (" + strref(n) + ")"
    if temporary:
        text = "<c\x01f\xff>" + text + " (" + strref(STR_TEMPORARY) + ")</c>"
    return text


def locstring(loc, strref: Callable[[int], str]) -> str:
    """The text of a localized string (its own text first, else its TLK entry); '' if it has none."""
    if not isinstance(loc, dict):
        return loc if isinstance(loc, str) else ""
    for key in sorted((k for k in loc if str(k).isdigit()), key=int):
        if isinstance(loc[key], str) and loc[key]:
            return loc[key]
    ref = loc.get("id")
    if isinstance(ref, int) and 0 <= ref < 0xFFFFFFFF:
        try:
            return strref(ref) or ""
        except Exception:
            return ""
    return ""


def unidentified_name(kind: str, strref: Optional[Callable[[int], str]] = None) -> str:
    """What an unidentified item is called, as the game client words it (CNWCItem::GetName(1), nwmain 8193.37): the
    base item's name, a space and dialog.tlk 5548 "(Unidentified)", e.g. "Ring (Unidentified)". `kind` is the base item's name (kind_name)."""
    suffix = ""
    if strref is not None:
        try:
            suffix = strref(STR_UNIDENTIFIED) or ""
        except Exception:
            suffix = ""
    return f"{kind or 'Item'} {suffix or '(Unidentified)'}"


def shown_name(item: dict, name: str, t: Optional[item_value.Tables], strref: Callable[[int], str], *, gm: bool = False) -> str:
    """The name the API sends for this item: its real `name` (item_name) when it is identified or `gm` asks for it, else unidentified_name,
    so a page never tells what an unidentified item is."""
    if gm or bool(_v(item.get("Identified", 1))):
        return name
    return unidentified_name(kind_name(item, t, strref) if t is not None else "", strref)


def kind_name(item: dict, t: item_value.Tables, strref: Callable[[int], str]) -> str:
    """The base item's name (baseitems.2da Name); '' when the base item is not known."""
    try:
        return strref(_ref(t, "baseitems", int(_v(item["BaseItem"])), "Name")) or ""
    except Exception:
        return ""


def level_required(item: dict, t: item_value.Tables, max_single: list[int]) -> int:
    """The level an item needs under the server's item level restriction: CNWSItem::GetMinEquipLevel (item_value.min_equip_level)."""
    return item_value.min_equip_level(item, t, max_single)


def examine(item: dict, t: item_value.Tables, strref: Callable[[int], str], max_single: list[int], *, ilr: bool = True,
            character_level: Optional[int] = None, gm: bool = False, no_base_item_line: frozenset = frozenset()) -> dict:
    """The examine panel of one item: {"identified", "type": [lines under the name], "description", "stats": [lines], "propertiesHeader",
    "properties": [lines], "level": {"required", "tooLow"} or None}.

    Unidentified: unless `gm`, the panel shows nothing that hints at what the item is worth: only the base item (its kind lines, its own
    description, "Base Item:") and its weight; no value, charges, level or properties. The weight is the real one, as the game has it
    (item_weight: weight reduction and increase properties count, though the properties stay hidden). With `gm` the panel is shown as if it
    were identified, and `identified` still says it is not.

    The level, only when the server's item level restriction is on (`ilr`; otherwise no "Level Required" line and `level` is None):
    `required` is what a player reads when examining it (level_required). `tooLow` is worked out only against a character's total level
    (`character_level`, CNWSCreatureStats::GetLevel(0)): too low when it is below (SendServerToPlayerExamineGui_ItemData, nwserver 8193.37
    0x5b55f0); otherwise None.

    `no_base_item_line`: base items whose panel has no "Base Item:" line (a server's own base items may want that); none by default."""
    identified = bool(_v(item.get("Identified", 1)))
    hidden = not identified and not gm
    if not identified and gm:
        item = {**item, "Identified": {"type": "byte", "value": 1}}
    base = _v(item["BaseItem"])
    if base == ARMOR_BASE_ITEM:
        ac = item_value.armor_ac(item, t)
        kind = [strref(_ref(t, "armor", ac, "DESCRIPTIONS")), strref(_ref(t, "armor", ac, "BASEITEMSTATREF"))]
    else:
        kind = [strref(_ref(t, "baseitems", base, "Name")), strref(_ref(t, "baseitems", base, "BaseItemStatRef"))]
    stats = []
    if base not in no_base_item_line:
        stats.append(f"{strref(STR_BASE_ITEM)} {strref(_ref(t, 'baseitems', base, 'Name'))}")
    if hidden:
        w = item_weight(item, t)
        stats.append(f"{strref(STR_WEIGHT)} {w // 10}.{w % 10}")
        return {"identified": False, "type": [x for x in kind if x], "description": strref(_ref(t, "baseitems", base, "Description")),
                "stats": stats, "propertiesHeader": "", "properties": [], "level": None}
    # SendServerToPlayerExamineGui_ItemData: an identified item shows its identified description (else the plain one).
    plain, full = locstring(_v(item.get("Description")), strref), locstring(_v(item.get("DescIdentified")), strref)
    # A description written on the item (SetDescription: the quill) is kept as an override and wins over the item's own text.
    description = _v(item.get("DescIdentifiedOv")) or full or plain
    charges = _v(item.get("Charges", 0)) or 0
    if charges > 0:
        stats.append(f"{strref(STR_CHARGES)} {charges}")
    w = item_weight(item, t)
    stats.append(f"{strref(STR_WEIGHT)} {w // 10}.{w % 10}")
    stats.append(f"{strref(STR_VALUE)} {strref(STR_GP)} {item_value.get_cost(item, t, 1, 0, 0, 0)}")
    level = None
    if ilr:
        need = level_required(item, t, max_single)
        stats.append(f"{strref(STR_LEVEL)} {need}")
        level = {"required": need, "tooLow": (character_level < need) if character_level is not None else None}
    props: list[str] = []
    # Active properties are always sent; of the passive ones the server leaves out weight increase and visual effect.
    for p in _props(item):
        ptype, sub, _ctab, cval, p1val = _prop(p)
        if ptype in UNSENT:
            continue
        s = property_text(ptype, sub, cval, p1val, base, t, strref)
        if s is not None:
            props.append(s)
    props.sort(key=lambda s: s.encode("utf-8", "replace"))      # SortProperties: strcmp order
    return {"identified": identified, "type": [x for x in kind if x], "description": description, "stats": stats,
            "propertiesHeader": strref(STR_PROPERTIES) if props else "", "properties": props, "level": level}
