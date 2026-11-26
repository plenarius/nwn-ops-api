"""item_examine: the examine panel's wording and an item's weight (the port's weights were checked against a real server on 1127 items)."""
from app.gamedata import item_examine as ex
from app.gamedata import item_value as iv

TLK = {1: "Enhancement Bonus:", 2: "Damage Bonus:", 3: "Fire", 4: "Armor Bonus:", 5: "+", 6: "+3", 7: "d6", 8: "1d6", 9: "Dodge", 10: "Poison", 11: "DC=", 12: "20",
       17: "Sword", ex.STR_WEIGHT: "Weight:", ex.STR_GP: "GP:", ex.STR_VALUE: "Value", ex.STR_BASE_ITEM: "Base Item:", ex.STR_PROPERTIES: "Special Properties:",
       ex.STR_CHARGES: "Charges:", ex.STR_LEVEL: "Level Required:", ex.STR_TEMPORARY: "Temporary"}


def N(v):
    return {"type": "int", "value": v}


def strref(n):
    return TLK.get(n, "")


def tables(extra=None):
    t = {
        "itempropdef": {0: {"GameStrRef": "1", "SubTypeResRef": "****", "CostTableResRef": "1", "Param1ResRef": "****"},
                        1: {"GameStrRef": "4", "SubTypeResRef": "****", "CostTableResRef": "1", "Param1ResRef": "****"},
                        7: {"GameStrRef": "2", "SubTypeResRef": "iprp_damagetype", "CostTableResRef": "****", "Param1ResRef": "****"},
                        9: {"GameStrRef": "10", "SubTypeResRef": "****", "CostTableResRef": "****", "Param1ResRef": "0"},
                        50: {"GameStrRef": "****", "SubTypeResRef": "****", "CostTableResRef": "****", "Param1ResRef": "****"}},
        "iprp_costtable": {1: {"Name": "IPRP_BONUSCOST"}},
        "iprp_bonuscost": {3: {"Name": "6"}},
        "iprp_damagetype": {2: {"Name": "3", "CostTableResRef": "2"}},
        "iprp_paramtable": {0: {"TableResRef": "IPRP_POISON"}},
        "iprp_poison": {1: {"Name": "12"}},
        "iprp_acmodtype": {0: {"Name": "9"}},
        "baseitems": {5: {"Name": "17", "AC_Enchant": "0", "TenthLBS": "30", "Container": "0", "ModelType": "0"}},
    }
    t["iprp_costtable"][2] = {"Name": "IPRP_DAMAGECOST"}
    t["iprp_damagecost"] = {4: {"Name": "8"}}
    t.update(extra or {})
    return iv.Tables(lambda n: t.get(n))


def test_a_property_is_its_name_then_each_part_after_a_space():
    t = tables()
    assert ex.property_text(0, 0, 3, 0, 5, t, strref) == "Enhancement Bonus: +3"                  # type, then the cost value
    assert ex.property_text(7, 2, 4, 0, 5, t, strref) == "Damage Bonus: Fire 1d6"               # type, subtype, the subtype's own cost table
    assert ex.property_text(9, 0, 0, 1, 5, t, strref) == "Poison 20"                              # a parameter table
    assert ex.property_text(50, 0, 0, 0, 5, t, strref) is None                                    # no name: the client adds nothing


def test_armor_bonus_names_the_kind_of_ac_for_the_item():
    assert ex.property_text(1, 0, 3, 0, 5, tables(), strref) == "Armor Bonus: +3 (Dodge)"


def test_a_table_the_client_cannot_load_drops_the_property():
    t = tables({"iprp_bonuscost": None})
    assert ex.property_text(0, 0, 3, 0, 5, t, strref) is None


def test_temporary_is_marked_in_colour():
    s = ex.property_text(0, 0, 3, 0, 5, tables(), strref, temporary=True)
    assert s == "<c\x01f\xff>Enhancement Bonus: +3 (Temporary)</c>"


def test_weight_reduction_and_increase_and_stack():
    t = tables({"iprp_weightcost": {1: {"Value": "0.8"}}, "iprp_costtable": {1: {"Name": "IPRP_WEIGHTCOST"}}, "iprp_weightinc": {2: {"Value": "50"}}})
    item = {"BaseItem": N(5), "StackSize": N(1), "PropertiesList": {"type": "list", "value": [
        {"PropertyName": N(11), "CostTable": N(1), "CostValue": N(1)}]}}
    assert ex.item_weight(item, t) == 24                                    # 30 tenths x 0.8
    item["PropertiesList"]["value"].append({"PropertyName": N(81), "Param1Value": N(2)})
    assert ex.item_weight(item, t) == 64                                    # (30 + 50) x 0.8
    item["StackSize"] = N(3)
    assert ex.item_weight(item, t) == 192


def test_properties_are_listed_in_strcmp_order():
    t = tables({"baseitems": {5: {"Name": "17", "BaseCost": "10", "ItemMultiplier": "1", "Stacking": "1", "TenthLBS": "30", "Container": "0", "ModelType": "0", "Category": "0"}},
                "itemvalue": {1: {"MAXSINGLEITEMVALUE": "1000"}}})
    item = {"BaseItem": N(5), "Identified": N(1), "PropertiesList": {"type": "list", "value": [
        {"PropertyName": N(7), "Subtype": N(2), "CostValue": N(4)}, {"PropertyName": N(0), "CostValue": N(3)}]}}
    out = ex.examine(item, t, strref, [1000])
    assert out["properties"] == ["Damage Bonus: Fire 1d6", "Enhancement Bonus: +3"]
    assert out["propertiesHeader"] == "Special Properties:"
    assert out["stats"][0] == "Base Item: Sword" and out["stats"][1].startswith("Weight: 3.0")
    item["Identified"] = N(0)
    assert ex.examine(item, t, strref, [1000])["properties"] == []     # the server sends none for an unidentified item


def test_a_description_written_with_the_quill_wins_over_the_items_own():
    t = tables({"baseitems": {5: {"Name": "17", "BaseCost": "10", "ItemMultiplier": "1", "Stacking": "1", "TenthLBS": "30", "Container": "0", "ModelType": "0", "Category": "0"}},
                "itemvalue": {1: {"MAXSINGLEITEMVALUE": "1000"}}})
    item = {"BaseItem": N(5), "Identified": N(1), "DescIdentified": {"type": "cexolocstring", "value": {"0": "Its own text"}},
            "DescIdentifiedOv": {"type": "cexostring", "value": "Written on it"}}
    assert ex.examine(item, t, strref, [1000])["description"] == "Written on it"
    item["DescIdentifiedOv"]["value"] = ""
    assert ex.examine(item, t, strref, [1000])["description"] == "Its own text"
    # Unidentified: the base item's own description, never the item's (nothing of what it is).
    item["Identified"] = N(0)
    item["Description"] = {"type": "cexolocstring", "value": {"0": "A sword that hums"}}
    item["DescriptionOverr"] = {"type": "cexostring", "value": "Unidentified, written"}
    t.table("baseitems")[5]["Description"] = "17"
    assert ex.examine(item, t, strref, [1000])["description"] == "Sword"
    assert ex.examine(item, t, strref, [1000], gm=True)["description"] == "Its own text"      # gm: as identified


def test_without_the_item_level_restriction_there_is_no_level_line():
    t = tables({"baseitems": {5: {"Name": "17", "BaseCost": "10", "ItemMultiplier": "1", "Stacking": "1", "TenthLBS": "30", "Container": "0", "ModelType": "0", "Category": "0"}},
                "itemvalue": {1: {"MAXSINGLEITEMVALUE": "1000"}}})
    item = {"BaseItem": N(5), "Identified": N(1)}
    on = ex.examine(item, t, strref, [1000], character_level=0)
    assert on["level"] == {"required": 1, "tooLow": True} and "Level Required: 1" in on["stats"]
    off = ex.examine(item, t, strref, [1000], ilr=False)
    assert off["level"] is None and not any(s.startswith("Level Required") for s in off["stats"])
    assert ex.examine(item, t, strref, [1000], no_base_item_line=frozenset({5}))["stats"][0].startswith("Weight:")
