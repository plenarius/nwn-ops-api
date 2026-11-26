"""The file readers (GFF, TLK, 2DA, ERF, KEY/BIF) and the resource lookup's order, on files made by tests/gamedata_files.py."""

import os

import pytest

from app.gamedata import gff
from app.gamedata.resman import Resources, erf_entries, key_entries
from app.gamedata.tables import CUSTOM_TLK_OFFSET, TwoDA, parse_2da_text, parse_tlk
from tests.gamedata_files import character, make_game, write_erf, write_gff, write_tlk


def test_gff_reads_back_every_field_type_in_nwn_gffs_shape():
    doc = character()
    got = gff.read(write_gff(doc))
    assert got["__data_type"] == "BIC "
    assert got["FirstName"] == {"type": "cexolocstring", "value": {"0": "Testa"}}
    assert got["CombatInfo"]["__struct_id"] == 51882 and got["CombatInfo"]["value"]["NumAttacks"]["value"] == 1
    assert got["SQLite"]["value"]["Data"] == {"type": "void", "value64": "AAEC"}
    assert got["Big"]["value"] == 2**40 and got["Neg"]["value"] == -5 and got["Sh"]["value"] == -3 and got["Ch"]["value"] == -1
    assert got["Lvl"]["value"] == 1.5 and got["Dbl"]["value"] == 0.25
    assert [i["__struct_id"] for i in got["Equip_ItemList"]["value"]] == [0x10, 0x80, 0x20000]
    assert got["Equip_ItemList"]["value"][0]["Description"]["value"] == {"id": 23}


def test_gff_refuses_what_is_not_gff():
    with pytest.raises(ValueError):
        gff.read(b"nonsense" * 10)


def test_locstring_helpers():
    assert gff._locstring({"type": "cexolocstring", "value": {"id": 5, "2": "B", "0": ""}}) == "B"
    assert gff._locstring({"type": "cexolocstring", "value": {"id": 5}}) == ""
    assert gff._v({"type": "int", "value": 3}) == 3 and gff._list({"type": "int", "value": 3}) == []


def test_tlk():
    rows = parse_tlk(write_tlk(["", "Hello", "Ça va"]))
    assert rows == ["", "Hello", "Ça va"]
    assert CUSTOM_TLK_OFFSET == 16777216
    with pytest.raises(ValueError):
        parse_tlk(b"XXXX" + b"\0" * 30)


def test_2da_quotes_empty_cells_short_rows_and_default_line():
    cols, rows = parse_2da_text('2DA V2.0\nDEFAULT: 0\n   Label Name   Cost\n0 Sword 12 ****\n1 "Two words" 13\n\n2 X\n')
    assert cols == ["Label", "Name", "Cost"]
    assert rows == [["Sword", "12", ""], ["Two words", "13", ""], ["X", "", ""]]
    t = TwoDA(cols, rows)
    assert t.get(0, "name") == "12" and t.get(0, "Cost") is None and t.get(5, "Name") is None and t.get_int(1, "NAME") == 13
    assert t.row(1) == {"Label": "Two words", "Name": "13", "Cost": None} and t.row(3) is None
    assert t.as_dict()[0] == {"Label": "Sword", "Name": "12", "Cost": ""}
    with pytest.raises(ValueError):
        parse_2da_text("not a table")


def test_erf_and_key_list_their_files(tmp_path):
    p = tmp_path / "a.hak"
    p.write_bytes(write_erf({"Feat.2da": b"2DA", "x.uti": b"GFF"}))
    assert [(r, t, s) for r, t, _o, s in erf_entries(p)] == [("feat", 2017, 3), ("x", 2025, 3)]
    g = make_game(tmp_path / "g")
    entries = key_entries(g["nwn"] / "data" / "nwn_base.key", g["nwn"], g["nwn"] / "lang" / "en")
    assert {(e[0], e[1]) for e in entries} >= {("classes", 2017), ("tst_sword", 2025)}


def test_resources_take_the_first_source_in_the_games_order(tmp_path):
    g = make_game(tmp_path)
    ovr = tmp_path / "override"
    ovr.mkdir()
    (ovr / "classes.2da").write_bytes(b"2DA V2.0\n\nLabel\n0 FromOverride\n")
    (ovr / "tst_sword.uti").write_bytes(b"override sword")
    res = Resources(g["nwn"], "en", g["hak"], ("tst_top", "tst_low"), g["module"], ovr)
    assert res.refresh() is True and res.refresh() is False
    assert res.find("feat", "2da").source == "tst_top"            # the first hak listed wins over the second
    assert res.find("tst_lowonly", "uti").source == "tst_low"
    assert res.find("tst_sword", "uti").source == "tst_top"       # a hak wins over the override folder and the base game
    assert res.find("tst_modring", "uti").source == "module"
    assert res.find("classes", "2da").source == "override"        # the override folder wins over the base game
    assert res.read("classes", "2da").startswith(b"2DA V2.0\n\nLabel")
    assert res.find("racialtypes", "2da").source == "base"
    assert res.find("TST_SWORD", "uti") is not None and res.find("nothing", "uti") is None
    assert "tst_basering" in res.names("uti")


def test_resources_see_a_changed_hak(tmp_path):
    g = make_game(tmp_path)
    res = Resources(g["nwn"], "en", g["hak"], ("tst_low",))
    res.refresh()
    assert res.find("tst_lowonly", "uti") is not None
    (g["hak"] / "tst_low.hak").write_bytes(write_erf({"feat.2da": b"2DA V2.0\n"}))
    st = os.stat(g["hak"] / "tst_low.hak")
    os.utime(g["hak"] / "tst_low.hak", ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    assert res.refresh() is True and res.find("tst_lowonly", "uti") is None


def test_a_language_key_and_bif_win_over_the_roots(tmp_path):
    from tests.gamedata_files import twoda, write_key_bif
    g = make_game(tmp_path)
    lang = g["nwn"] / "lang" / "en"
    write_key_bif(lang, "nwn_base", "data/base.bif", {"skills.2da": twoda(["Label"], [["Localized"]])}, key_dir=lang / "data")
    res = Resources(g["nwn"], "en")
    res.refresh()
    assert b"Localized" in res.read("skills", "2da")
    assert res.find("classes", "2da") is None     # the language's key replaces the root's key of the same name


def test_an_unreadable_hak_is_skipped(tmp_path, caplog):
    g = make_game(tmp_path)
    (g["hak"] / "broken.hak").write_bytes(b"not an erf at all" * 20)
    res = Resources(g["nwn"], "en", g["hak"], ("broken", "tst_low", "missing"))
    res.refresh()
    assert res.find("tst_lowonly", "uti").source == "tst_low"
    assert "broken.hak" in caplog.text
