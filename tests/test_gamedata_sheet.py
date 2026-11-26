"""GameData (the module's hak list and custom talk table, reloads) and the character sheet, on a made-up game and character."""

import os

import pytest

from app.gamedata import bic, data, gff
from app.gamedata.data import GameData
from tests.conftest import make_settings
from tests.gamedata_files import CUSTOM, DIALOG, character, make_game, write_gff, write_tlk


@pytest.fixture
def game(tmp_path):
    g = make_game(tmp_path)
    s = make_settings(game_nwn_root=str(g["nwn"]), game_hak_dir=str(g["hak"]), game_tlk_dir=str(g["tlk"]), game_module_file=str(g["module"]))
    gd = GameData(s)
    gd.ensure()
    return gd, g


def test_the_module_names_the_haks_and_the_custom_talk_table(game):
    gd, _ = game
    assert gd.res.haks == ("tst_top", "tst_low") and gd.custom_tlk == "tst_custom"
    assert gd.strref(1) == "Fighter" and gd.strref(16777216) == "Rune Knight" and gd.strref(16777216 + 99) == ""
    assert gd.strref(-1) == "" and gd.strref(None) == "" and gd.strref(len(DIALOG)) == ""
    assert gd.named("classes", 2) == "Rune Knight" and gd.named("classes", 9) == ""
    assert gd.named("feat", 2, "FEAT", "LABEL") == "Rune Sight"                         # the hak's feat.2da, the custom talk table
    assert gd.named("feat", 9, "FEAT", "LABEL") == ""
    assert gd.source("feat", "2da") == "tst_top" and gd.twoda("nothing") is None
    assert gd.max_single() == [1000, 5000, 20000]


def test_settings_win_over_the_module(tmp_path):
    g = make_game(tmp_path)
    s = make_settings(game_nwn_root=str(g["nwn"]), game_hak_dir=str(g["hak"]), game_haks="tst_low", game_custom_tlk="other.tlk",
                      game_module_file=str(g["module"]))
    gd = GameData(s)
    gd.ensure()
    assert gd.res.haks == ("tst_low",) and gd.custom_tlk == "other" and gd.custom == []
    assert gd.source("feat", "2da") == "tst_low"


def test_without_a_module_or_haks_only_the_base_game(tmp_path):
    g = make_game(tmp_path)
    gd = GameData(make_settings(game_nwn_root=str(g["nwn"])))
    gd.ensure()
    assert gd.res.haks == () and gd.source("feat", "2da") == "base" and gd.blueprint("tst_sword")["LocalizedName"]["value"]["0"] == "Rusty Blade"


def test_a_changed_talk_table_is_read_again(game, monkeypatch):
    gd, g = game
    p = g["tlk"] / "tst_custom.tlk"
    p.write_bytes(write_tlk(["Rune Lord"] + CUSTOM[1:]))
    st = os.stat(p)
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    gd.ensure()
    assert gd.strref(16777216) == "Rune Knight"            # not looked at again within CHECK_EVERY
    monkeypatch.setattr(data, "CHECK_EVERY", 0.0)
    gd.ensure()
    assert gd.strref(16777216) == "Rune Lord"


def test_alignment_names():
    assert bic.alignment_label(50, 50) == "True Neutral"
    assert bic.alignment_label(80, 20) == "Lawful Evil"
    assert bic.alignment_label(10, 90) == "Chaotic Good"
    assert bic.alignment_label(50, 70) == "Neutral Good"


def test_the_character_sheet(game):
    gd, _ = game
    sh = bic.sheet(gff.read(write_gff(character())), gd)
    assert sh["name"] == {"first": "Testa", "last": "Example", "full": "Testa Example"}
    assert sh["gender"] == {"id": 1, "name": "Female"} and sh["race"] == {"id": 1, "name": "Elf"}
    assert sh["alignment"]["name"] == "Lawful Evil" and sh["deity"] == "The Test" and sh["level"] == 3
    assert sh["classes"] == [{"id": 0, "name": "Fighter", "level": 1}, {"id": 2, "name": "Rune Knight", "level": 2}]
    # Str: 14 base, Rune Knight's stat gain table +1, the sword +1.
    assert sh["abilities"]["STR"] == {"base": 14, "racial": 0, "classes": 1, "items": 1, "score": 16, "modifier": 3}
    assert sh["abilities"]["DEX"]["racial"] == 2 and sh["abilities"]["CON"] == {"base": 13, "racial": -2, "classes": 1, "items": 0,
                                                                                  "score": 12, "modifier": 1}
    assert sh["abilities"]["WIS"]["modifier"] == -1
    assert sh["skills"] == [{"id": 0, "name": "Spot", "ranks": 4, "key_ability": "WIS"}]
    assert sh["feats"] == [{"id": 2, "name": "Rune Sight"}, {"id": 1, "name": "Toughness"}]
    assert sh["hit_points"] == {"current": 20, "max": 24} and sh["experience"] == 3000
    assert sh["portrait"] == "po_el_f_02_"
    blade, ring = sh["equipped"]                                                      # the creature hide (0x20000) is left out
    assert blade["slot"] == "right_hand" and blade["name"] == "Testa's Blade" and blade["base_item"] == {"id": 0, "name": "Longsword"}
    assert blade["examine"]["properties"] == ["Ability Bonus: Strength +1"]
    assert blade["examine"]["level"] == {"required": 2, "tooLow": False}
    assert ring["slot"] == "left_ring" and ring["name"] == "Ring (Unidentified)" and ring["identified"] is False
    assert ring["examine"]["properties"] == [] and ring["examine"]["level"] is None


def test_a_portrait_named_in_the_file_and_a_name_given_in_game(game):
    gd, _ = game
    doc = character(Portrait={"type": "resref", "value": "PO_Custom_"})
    doc["Equip_ItemList"]["value"][0]["DisplayName"] = {"type": "cexostring", "value": "Renamed"}
    sh = bic.sheet(gff.read(write_gff(doc)), gd)
    assert sh["portrait"] == "po_custom_" and sh["equipped"][0]["name"] == "Renamed"


def test_module_ifo_missing(tmp_path):
    from tests.gamedata_files import write_erf
    p = tmp_path / "empty.mod"
    p.write_bytes(write_erf({"x.uti": b"GFF"}, kind=b"MOD "))
    with pytest.raises(ValueError):
        data.module_ifo(p)
