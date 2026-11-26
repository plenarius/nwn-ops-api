"""A character's Use Magic Device skill rank, as the server computes it (`CNWSCreatureStats::GetSkillRank` for skill 19).

Ported from nwserver-linux 8193.37 (GetSkillRank, GetCHAStat, GetTotalEffectBonus, the item property handlers) and checked against the
engine on generated characters.

The rank is the saved ranks, plus the skill's bonus effects, plus the modifier of its key ability (Charisma for Use Magic Device),
plus Skill Focus feats, less negative levels. The ability's score is the saved base, plus its race's adjustment (racialtypes.2da), plus
the ability effects, plus what the classes add (at least 3); the modifier is (score - 10) / 2 rounded down.

Effects come from the worn items' properties: Ability Bonus and Decreased Ability Score, Skill Bonus and Decreased Skill. A source
(an item, or a spell) counts once, at its largest effect of the kind; the sources add; the bonuses and the penalties are each capped
by the server's limits (12 and 30 for abilities, 50 for skills); an ability penalty is cut when it is applied so that the score does not fall below 3,
so the order the server applies the items in (the .bic's Equip_ItemList order) matters. Effects that the .bic does not hold (spells, and what scripts apply at login) are not counted.
"""

from __future__ import annotations

from app.gamedata.gff import _v
from app.gamedata.item_value import Tables

SKILL_USE_MAGIC_DEVICE = 19
ABILITY_CHA = 5
ABILITY_BONUS, DECREASED_ABILITY, SKILL_BONUS, DECREASED_SKILL = 0, 27, 52, 29   # item property types
# the server's limits on the sum of bonuses and of penalties
ABILITY_BONUS_LIMIT, ABILITY_PENALTY_LIMIT, SKILL_BONUS_LIMIT = 12, 30, 50
SKILL_FOCUS = {SKILL_USE_MAGIC_DEVICE: (0xC1, 0x261)}     # feat, epic feat; +3 and +10 (ruleset.2da)


def _props(item: dict, ptype: int) -> list:
    return [p for p in (_v(item.get("PropertiesList")) or []) if _v(p["PropertyName"]) == ptype]


def _bonus_value(p: dict, t: Tables, table: int) -> int:
    name = t.cost_table(table)
    v = t.int(name, _v(p.get("CostValue", 0)), "Value") if name else None
    return v or 0


def ability_effects(items: list, ability: int, base_score: int, t: Tables, immune: bool = False) -> int:
    """The net effect of the worn items on an ability score. `items` are in the order the server applies them (the order of the
    .bic's Equip_ItemList). An item counts once, at its largest bonus and its largest penalty; a penalty is cut when it is applied
    so that the score does not fall below 3; the bonuses are capped at +12 and the penalties at 30. A creature immune to
    ability decreases (a construct, undead or ooze) takes no penalties; a player character does not."""
    stored: list[list[int]] = []     # per item: [largest bonus, largest applied penalty]

    def net() -> int:
        return min(sum(b for b, _ in stored), ABILITY_BONUS_LIMIT) - min(sum(p for _, p in stored), ABILITY_PENALTY_LIMIT)

    for it in items:
        cur = [0, 0]
        stored.append(cur)
        for p in _v(it.get("PropertiesList")) or []:
            if _v(p.get("Subtype", 0)) != ability:
                continue
            kind = _v(p["PropertyName"])
            if kind == ABILITY_BONUS:
                cur[0] = max(cur[0], _bonus_value(p, t, 1))
            elif kind == DECREASED_ABILITY and not immune:
                room = max(3, base_score + net()) - 3
                cur[1] = max(cur[1], max(0, min(-_bonus_value(p, t, 21), room)))
    return net()


def skill_effects(items: list, skill: int, t: Tables, limit: int = SKILL_BONUS_LIMIT) -> int:
    bonus = penalty = 0
    for it in items:
        bonus += max([_v(p.get("CostValue", 0)) for p in _props(it, SKILL_BONUS) if _v(p.get("Subtype", 0)) == skill] or [0])
        penalty += max([-_bonus_value(p, t, 21) for p in _props(it, DECREASED_SKILL) if _v(p.get("Subtype", 0)) == skill] or [0])
    return min(bonus, limit) - min(penalty, limit)


def stat_modifier(score: int) -> int:
    return (score - 10) // 2


ABILITY_COLUMNS = ["Str", "Dex", "Con", "Int", "Wis", "Cha"]


def class_adjust(gff: dict, ability: int, t: Tables) -> int:
    """GetClassesAbilityAdjust: what the character's classes add to an ability (Dragon Disciple, Pale Master): for each class the sum of
    its stat gain table's column for the ability over levels 1..level (the table is classes.2da's StatGainTable)."""
    total = 0
    for c in _v(gff.get("ClassList")) or []:
        table = t.cell("classes", _v(c["Class"]), "StatGainTable")
        if not table:
            continue
        for level in range(1, (_v(c["ClassLevel"]) or 0) + 1):
            total += t.int(table.lower(), level - 1, ABILITY_COLUMNS[ability]) or 0
    return max(-128, min(127, total))


def ability_parts(gff: dict, ability: int, items: list, t: Tables, immune: bool = False) -> dict:
    """One ability score as the server works it out (GetSTRStat .. GetCHAStat) from what the file holds: {base, racial, classes, items,
    score, modifier}. `items` are the worn items in Equip_ItemList order."""
    race = _v(gff.get("Race", 0))
    base = _v(gff.get(ABILITY_COLUMNS[ability], 10))
    racial = t.int("racialtypes", race, ABILITY_COLUMNS[ability] + "Adjust") or 0
    classes = class_adjust(gff, ability, t)
    before = base + racial + classes
    effects = ability_effects(items, ability, before, t, immune)
    score = max(3, before + effects)
    return {"base": base, "racial": racial, "classes": classes, "items": effects, "score": score, "modifier": stat_modifier(score)}


def cha_score(gff: dict, items: list, t: Tables, immune: bool = False) -> int:
    return ability_parts(gff, ABILITY_CHA, items, t, immune)["score"]


def umd_rank(gff: dict, worn: list, t: Tables, immune: bool = False) -> int:
    """The character's Use Magic Device rank with `worn` (the item structs in their equipment slots). 0 if they cannot use the skill."""
    skills = _v(gff.get("SkillList")) or []
    base = _v(skills[SKILL_USE_MAGIC_DEVICE]["Rank"]) if len(skills) > SKILL_USE_MAGIC_DEVICE else 0
    if base == 0 and not (t.int("skills", SKILL_USE_MAGIC_DEVICE, "Untrained") or 0):
        return 0
    feats = {_v(f["Feat"]) for f in (_v(gff.get("FeatList")) or [])}
    rank = base + skill_effects(worn, SKILL_USE_MAGIC_DEVICE, t) + stat_modifier(cha_score(gff, worn, t, immune))
    focus, epic = SKILL_FOCUS[SKILL_USE_MAGIC_DEVICE]
    rank += 3 if focus in feats else 0
    rank += 10 if epic in feats else 0
    return max(-127, min(127, rank))
