"""An item's value as the server computes it, and what it is used for: the minimum level to wear it and the Use Magic Device cost.

A port of the server's `CNWSItem::CalculateBaseCosts`, `CalculatePassiveCost` and `GetCost` (nwserver 8193.37), checked against the engine
on every item blueprint of the base game and of a large persistent world's module.

An item is the GFF-JSON struct of a blueprint or of an inventory item (BaseItem, StackSize, AddCost, Identified, Plot, Charges,
PropertiesList, ArmorPart_Torso). `Tables` answers questions about the 2DAs, as the game finds them (the haks' over the base game's).
"""

from __future__ import annotations

import struct
from typing import Callable, Optional

from app.gamedata.gff import _v

CAST_SPELL = 15
PER_CHARGE_VALUES = range(2, 7)       # iprp_chargecost rows whose cost is a share of the charges left
# ruleset.2da MAX_CHARGES_FOR_ITEM_COST: the charges that make a "per charge" spell worth its full cost (the engine's default is 50, which
# the stock ruleset.2da keeps). CalculateBaseCosts reads it with GetRulesetIntEntry(.., 50) and keeps it within 1..250.
MAX_CHARGES_RULE, MAX_CHARGES_DEFAULT = "MAX_CHARGES_FOR_ITEM_COST", 50


def f32(x: float) -> float:
    """Round to a 32-bit float, as the engine's arithmetic does."""
    return struct.unpack("<f", struct.pack("<f", x))[0]


def trunc(x: float) -> int:
    return int(x)


class Tables:
    """2DA lookups. `load(name)` returns {row: {column: text}} for a table (lower case name) or None."""

    def __init__(self, load: Callable[[str], Optional[dict]]):
        self._load, self._cache = load, {}

    def table(self, name: str) -> Optional[dict]:
        name = name.lower()
        if name not in self._cache:
            self._cache[name] = self._load(name)
        return self._cache[name]

    def cell(self, table: str, row: int, col: str) -> Optional[str]:
        t = self.table(table)
        v = t.get(row, {}).get(col) if t else None
        return None if v in (None, "", "****") else v

    def float(self, table: str, row: int, col: str) -> Optional[float]:
        v = self.cell(table, row, col)
        try:
            return f32(float(v)) if v is not None else None
        except ValueError:
            return None

    def int(self, table: str, row: int, col: str) -> Optional[int]:
        v = self.cell(table, row, col)
        try:
            return int(v, 0) if v is not None else None
        except ValueError:
            return None

    def cost_table(self, n: int) -> Optional[str]:
        """IPRPCostTable(n): the 2DA iprp_costtable.2da names for row n."""
        name = self.cell("iprp_costtable", n, "Name")
        return name.lower() if name else None


def armor_ac(item: dict, t: Tables) -> int:
    """The armor class the engine keeps in an armor item (the whole part of parts_chest.2da's ACBONUS for its torso)."""
    torso = _v(item.get("ArmorPart_Torso", item.get("xArmorPart_Torso", 0))) or 0
    v = t.float("parts_chest", torso, "ACBONUS")
    return trunc(v) if v is not None else 0


def passive_cost(prop: dict, t: Tables) -> float:
    """CalculatePassiveCost: the property type's Cost (else its subtype's) times the cost table's value for the property."""
    ptype, sub, cv = _v(prop["PropertyName"]), _v(prop.get("Subtype", 0)), _v(prop.get("CostValue", 0))
    cost = t.float("itempropdef", ptype, "Cost")
    if cost is None:
        subtable = t.cell("itempropdef", ptype, "SubTypeResRef")
        if subtable:
            c2 = t.float(subtable.lower(), sub, "Cost")
            if c2 is not None:
                cost = c2
    ctn = t.int("itempropdef", ptype, "CostTableResRef")
    if ctn:
        name = t.cost_table(ctn)
        if name and t.table(name) is not None:
            c3 = t.float(name, cv, "Cost")
            # a blank Cost in either place counts as nothing: the property is then worth 0
            cost = f32((cost or 0.0) * (c3 or 0.0))
    return cost if cost is not None else 0.0


def ruleset_int(t: Tables, label: str, default: int) -> int:
    """CNWRules::GetRulesetIntEntry: the Value of ruleset.2da's row with this Label, else the default."""
    rules = getattr(t, "_ruleset", None)
    if rules is None:
        rules = {row.get("Label"): row.get("Value") for row in (t.table("ruleset") or {}).values()}
        t._ruleset = rules
    try:
        return int(str(rules[label]).strip('"'), 0)
    except (KeyError, ValueError):
        return default


def max_charges(t: Tables) -> int:
    """The divisor for charges in a spell's cost: MAX_CHARGES_FOR_ITEM_COST, at most 250 and at least 1 (CalculateBaseCosts)."""
    n = min(ruleset_int(t, MAX_CHARGES_RULE, MAX_CHARGES_DEFAULT), 250)
    return n if n > 0 else 1


def spell_cost(prop: dict, charges: int, t: Tables) -> float:
    """One Cast Spell property's cost: iprp_spells Cost times iprp_chargecost Cost, and for the "per charge" rows (2..6) times
    charges / MAX_CHARGES_FOR_ITEM_COST, the charges the item has now (single precision throughout)."""
    sub, cv = _v(prop.get("Subtype", 0)), _v(prop.get("CostValue", 0))
    spell = t.float("iprp_spells", sub, "Cost") or 0.0
    per = t.float("iprp_chargecost", cv, "Cost") or 0.0
    cost = f32(spell * per)
    if cv in PER_CHARGE_VALUES:
        cost = f32(f32(f32(float(charges)) / f32(float(max_charges(t)))) * cost)
    return cost


def base_costs(item: dict, t: Tables) -> tuple[int, int]:
    """CalculateBaseCosts: (the value of an unidentified item, the value of an identified one before AddCost)."""
    base = _v(item["BaseItem"])
    cat = t.int("baseitems", base, "Category")
    mult = t.float("baseitems", base, "ItemMultiplier")
    mult = 1.0 if mult is None else mult
    if cat == 4:
        c = t.float("armor", armor_ac(item, t), "COST")
        base_cost = c if c is not None else 0.0
    else:
        c = t.float("baseitems", base, "BaseCost")
        base_cost = c if c is not None else 0.0
    positive = negative = 0.0
    spells_sum = first = second = 0.0
    charges = _v(item.get("Charges", 0)) or 0
    for p in _v(item.get("PropertiesList")) or []:
        if _v(p["PropertyName"]) == CAST_SPELL:
            c = spell_cost(p, charges, t)
            spells_sum = f32(spells_sum + f32(c * 0.5))
            if c > first:
                first = c
            elif c > second:
                second = c
        else:
            c = passive_cost(p, t)
            if c > 0.0:
                positive = f32(positive + c)
            else:
                negative = f32(negative + c)
    spells = trunc(f32(f32(0.5 * first) + f32(0.25 * second) + spells_sum))
    unid = trunc(f32(f32(float(trunc(base_cost))) * mult))
    total = trunc(f32(f32(positive * positive) * 1000.0)) + spells + trunc(base_cost)
    sub = trunc(f32(f32(negative * negative) * 1000.0))
    ident = 0 if total < sub else trunc(f32(float(total - sub) * mult))
    return unid, ident


def get_cost(item: dict, t: Tables, p1: int, p2: int, p3: int, p4: int) -> int:
    """CNWSItem::GetCost(p1, p2, p3, p4)."""
    base = _v(item["BaseItem"])
    if p4 == 0:
        if _v(item.get("Plot", 0)) == 1:
            return 0
        if 0x45 <= base < 0x4A:
            return 0
    unid, ident = base_costs(item, t)
    identified = bool(_v(item.get("Identified", 1)))
    value = unid if (p2 == 0 and not identified) else (_v(item.get("AddCost", 0)) or 0) + ident
    if p3 == 1:
        value = value * (t.int("baseitems", base, "ILRStackSize") or 0)
    elif p1 == 1:
        n = _v(item.get("StackSize", 1)) or 1
        if value == 0:
            if n < (t.int("baseitems", base, "Stacking") or 0):
                return 0
        value = value * n
    return value if value != 0 else 1


def min_equip_level(item: dict, t: Tables, max_single_values: list[int]) -> int:
    """CNWSItem::GetMinEquipLevel: the first itemvalue.2da row (1-based) whose MAXSINGLEITEMVALUE covers GetCost(0,0,1,1), compared
    unsigned; past the last row, the number of rows plus one. GetCost(0,0,1,1) leaves out the plot rule, takes an unidentified item's
    unidentified value, and counts a stack as baseitems.2da ILRStackSize copies whatever its size (nwserver 8193.37: GetMinEquipLevel at
    0x538c10, GetCost at 0x537c00). NWNX_Item's MinEquipLevel override and modifier are not seen: they live in the running server."""
    cost = get_cost(item, t, 0, 0, 1, 1)
    for i, top in enumerate(max_single_values):
        if top >= cost:
            return i + 1
    return len(max_single_values) + 1
