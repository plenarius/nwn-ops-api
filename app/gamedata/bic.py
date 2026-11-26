"""A character file (.bic) as a character sheet: what the file holds, with names from the talk tables and 2DAs.

The file is read with gff.read (nwn_gff's JSON shape). Scores are worked out as the server does from what the file holds (skill_rank);
anything applied in the running game (spells, effects scripts add at login) is not in the file and not counted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.gamedata import item_examine, skill_rank
from app.gamedata.gff import _list, _locstring, _v

ABILITIES = ["STR", "DEX", "CON", "INT", "WIS", "CHA"]
GENDER_NAMES = {0: "Male", 1: "Female", 2: "Both", 3: "Other", 4: "None"}

# Equipment slots (the struct id of each Equip_ItemList entry) and their names. From the creature weapons up (0x4000) the slots hold the
# creature's own claws, bite and hide, not gear: left out of the sheet.
SLOTS = {0x1: "head", 0x2: "chest", 0x4: "boots", 0x8: "arms", 0x10: "right_hand", 0x20: "left_hand", 0x40: "cloak",
         0x80: "left_ring", 0x100: "right_ring", 0x200: "neck", 0x400: "belt", 0x800: "arrows", 0x1000: "bullets", 0x2000: "bolts"}
CREATURE_SLOTS = 0x4000


@dataclass
class CharacterClass:
    class_id: int
    level: int


@dataclass
class ParsedBic:
    first_name: str
    last_name: str
    description: str
    gender: int
    race_id: int
    subrace: str
    deity: str
    lawful_chaotic: int
    good_evil: int
    age: int
    hp_max: int
    hp_current: int
    experience: int
    portrait: str           # resref if the file names one
    portrait_id: Optional[int]  # else a row of portraits.2da
    classes: list[CharacterClass] = field(default_factory=list)
    feats: list[int] = field(default_factory=list)
    skills: list[int] = field(default_factory=list)  # ranks by skills.2da row

    @property
    def total_level(self) -> int:
        return sum(c.level for c in self.classes)

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


def _int(node, default: int = 0) -> int:
    v = _v(node)
    try:
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def parse(data: dict) -> ParsedBic:
    classes = [CharacterClass(_int(c.get("Class")), _int(c.get("ClassLevel"))) for c in _list(data.get("ClassList"))
               if _v(c.get("Class")) is not None]
    feats = [_int(f.get("Feat")) for f in _list(data.get("FeatList")) if _v(f.get("Feat")) is not None]
    skills = [_int(s.get("Rank")) for s in _list(data.get("SkillList"))]
    pid = _v(data.get("PortraitId"))
    return ParsedBic(
        first_name=_locstring(data.get("FirstName")),
        last_name=_locstring(data.get("LastName")),
        description=_locstring(data.get("Description")),
        gender=_int(data.get("Gender")),
        race_id=_int(data.get("Race")),
        subrace=str(_v(data.get("Subrace")) or "").strip(),
        deity=str(_v(data.get("Deity")) or "").strip(),
        lawful_chaotic=_int(data.get("LawfulChaotic")),
        good_evil=_int(data.get("GoodEvil")),
        age=_int(data.get("Age")),
        hp_max=_int(data.get("MaxHitPoints")) or _int(data.get("HitPoints")),
        hp_current=_int(data.get("CurrentHitPoints")),
        experience=_int(data.get("Experience")),
        portrait=str(_v(data.get("Portrait")) or "").strip(),
        portrait_id=int(pid) if pid is not None else None,
        classes=classes,
        feats=feats,
        skills=skills,
    )


def alignment_label(law: int, good: int) -> str:
    """NWN's 0-100 LawfulChaotic and GoodEvil as the game names the alignment (70 and up lawful or good, 30 and below chaotic or evil)."""
    def axis(v: int, low: str, mid: str, high: str) -> str:
        return high if v >= 70 else low if v <= 30 else mid
    lc, ge = axis(law, "Chaotic", "Neutral", "Lawful"), axis(good, "Evil", "Neutral", "Good")
    return "True Neutral" if lc == ge == "Neutral" else f"{lc} {ge}"


def item_name(item: dict, strref) -> str:
    """The name a player sees: a name given in the game (DisplayName) first, else the item's localized name, its own text or its TLK
    reference."""
    shown = _v(item.get("DisplayName"))
    if isinstance(shown, str) and shown.strip():
        return shown
    return item_examine.locstring(_v(item.get("LocalizedName")), strref)


def worn(data: dict) -> list[tuple[int, dict]]:
    """(slot, item) of the gear the character wears, in Equip_ItemList order (the order the server applies it in)."""
    return [(int(e.get("__struct_id", 0)), e) for e in _list(data.get("Equip_ItemList")) if 0 < int(e.get("__struct_id", 0)) < CREATURE_SLOTS]


def portrait_resref(p: ParsedBic, gd) -> str:
    """The portrait's resref without its size letter (po_hu_m_01_): the file's own, else portraits.2da's BaseResRef with "po_"."""
    if p.portrait:
        return p.portrait.lower()
    t = gd.twoda("portraits")
    base = t.get(p.portrait_id, "BaseResRef") if (t is not None and p.portrait_id is not None) else None
    return f"po_{base.lower()}" if base else ""


def sheet(data: dict, gd) -> dict:
    """The character sheet of a character file (gff JSON) against the game data `gd` (data.GameData)."""
    p = parse(data)
    t, strref = gd.tables, gd.strref
    gear = worn(data)
    items = [i for _, i in gear]
    abilities = {}
    for i, a in enumerate(ABILITIES):
        abilities[a] = skill_rank.ability_parts(data, i, items, t)
    skills_t = gd.twoda("skills")
    skills = []
    for sid, ranks in enumerate(p.skills):
        if ranks:
            key = (skills_t.get(sid, "KeyAbility") or "").upper() if skills_t else ""
            skills.append({"id": sid, "name": gd.named("skills", sid), "ranks": ranks, "key_ability": key or None})
    max_single = gd.max_single()
    equipped = []
    for slot, item in gear:
        base = _int(item.get("BaseItem"))
        kind = item_examine.kind_name(item, t, strref)
        equipped.append({
            "slot": SLOTS.get(slot, str(slot)),
            "resref": str(_v(item.get("TemplateResRef")) or ""),
            "name": item_examine.shown_name(item, item_name(item, strref), t, strref),
            "base_item": {"id": base, "name": kind},
            "identified": bool(_v(item.get("Identified", 1))),
            "examine": item_examine.examine(item, t, strref, max_single, ilr=gd.ilr, character_level=p.total_level),
        })
    return {
        "name": {"first": p.first_name, "last": p.last_name, "full": p.full_name},
        "description": p.description,
        "gender": {"id": p.gender, "name": GENDER_NAMES.get(p.gender, str(p.gender))},
        "race": {"id": p.race_id, "name": gd.named("racialtypes", p.race_id)},
        "subrace": p.subrace,
        "deity": p.deity,
        "alignment": {"law_chaos": p.lawful_chaotic, "good_evil": p.good_evil, "name": alignment_label(p.lawful_chaotic, p.good_evil)},
        "age": p.age,
        "level": p.total_level,
        "experience": p.experience,
        "hit_points": {"current": p.hp_current, "max": p.hp_max},
        "classes": [{"id": c.class_id, "name": gd.named("classes", c.class_id), "level": c.level} for c in p.classes],
        "abilities": abilities,
        "skills": skills,
        "feats": sorted(({"id": f, "name": gd.named("feat", f, "FEAT", "LABEL")} for f in p.feats), key=lambda x: x["name"].lower()),
        "equipped": equipped,
        "portrait": portrait_resref(p, gd),
    }
