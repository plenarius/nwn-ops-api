"""skill_rank: ability scores and the Use Magic Device rank from the file and the worn items, by the server's rules."""

from app.gamedata import item_value as iv
from app.gamedata import skill_rank as sr


def N(v):
    return {"type": "int", "value": v}


def L(items):
    return {"type": "list", "value": items}


T = iv.Tables(lambda n: {
    "iprp_costtable": {1: {"Name": "IPRP_BONUSCOST"}, 21: {"Name": "IPRP_NEG10COST"}},
    "iprp_bonuscost": {i: {"Value": str(i)} for i in range(13)},
    "iprp_neg10cost": {i: {"Value": str(-i)} for i in range(11)},
    "racialtypes": {0: {"ChaAdjust": "0", "StrAdjust": "2"}, 1: {"ChaAdjust": "-2", "StrAdjust": "0"}},
    "classes": {0: {"StatGainTable": "****"}, 1: {"StatGainTable": "CLS_STAT_X"}},
    "cls_stat_x": {0: {"Str": "1", "Cha": "0"}, 1: {"Str": "0", "Cha": "1"}, 2: {"Str": "1", "Cha": "0"}},
    "skills": {19: {"Untrained": "0"}},
}.get(n))


def item(*props):
    return {"PropertiesList": L([{"PropertyName": N(k), "Subtype": N(s), "CostValue": N(v)} for k, s, v in props])}


def pc(cha=10, race=0, classes=((0, 5),), umd=0, feats=()):
    skills = [{"Rank": N(0)} for _ in range(19)] + [{"Rank": N(umd)}]
    return {"Cha": N(cha), "Str": N(10), "Race": N(race), "ClassList": L([{"Class": N(c), "ClassLevel": N(lv)} for c, lv in classes]),
            "SkillList": L(skills), "FeatList": L([{"Feat": N(f)} for f in feats])}


def test_an_item_counts_once_at_its_largest_bonus_and_the_sum_is_capped():
    a = item((0, 5, 4), (0, 5, 6))         # two Cha bonuses on one item: only the 6
    b = item((0, 5, 5))
    assert sr.ability_effects([a], 5, 10, T) == 6
    assert sr.ability_effects([a, b, item((0, 5, 12))], 5, 10, T) == 12      # +12 at most


def test_a_penalty_stops_at_a_score_of_3():
    assert sr.ability_effects([item((27, 5, 10))], 5, 8, T) == -5
    assert sr.ability_effects([item((27, 5, 10))], 5, 8, T, immune=True) == 0


def test_skill_effects():
    assert sr.skill_effects([item((52, 19, 5)), item((52, 19, 3), (29, 19, 2))], 19, T) == 6


def test_class_stat_gains_add_by_level():
    assert sr.class_adjust(pc(classes=((1, 3),)), 0, T) == 2
    assert sr.class_adjust(pc(classes=((1, 2), (0, 9))), 5, T) == 1


def test_ability_parts():
    parts = sr.ability_parts(pc(race=0, classes=((1, 3),)), 0, [item((0, 0, 2))], T)
    assert parts == {"base": 10, "racial": 2, "classes": 2, "items": 2, "score": 16, "modifier": 3}
    assert sr.ability_parts(pc(cha=3, race=1), 5, [], T)["score"] == 3     # never below 3
    assert sr.stat_modifier(9) == -1 and sr.stat_modifier(3) == -4


def test_use_magic_device_rank():
    assert sr.umd_rank(pc(umd=0), [], T) == 0                               # untrained: no rank at all
    assert sr.umd_rank(pc(umd=5, cha=14), [item((52, 19, 2))], T) == 9      # 5 + 2 + Cha 14's 2
    assert sr.umd_rank(pc(umd=5, cha=14, feats=(0xC1, 0x261)), [], T) == 20  # Skill Focus +3, Epic +10
