"""The game data routes through the whole app: off without their settings, the read key opens them, retire needs the admin key."""

import base64
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.ssh import SshResult
from tests.conftest import API_KEY, make_settings
from tests.gamedata_files import character, make_game, write_gff
from tests.test_gamedata_vault import fake_ssh

ADMIN, TEAM, READ = "admin-key-0123456789", "team-key-0123456789", "read-key-0123456789"


def H(key):
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def client(tmp_path):
    g = make_game(tmp_path)
    s = make_settings(ops_api_admin_key=ADMIN, ops_api_team_key=TEAM, ops_api_read_key=READ, game_nwn_root=str(g["nwn"]),
                      game_hak_dir=str(g["hak"]), game_tlk_dir=str(g["tlk"]), game_module_file=str(g["module"]))
    with TestClient(create_app(s)) as c:
        yield c


def test_off_without_settings(monkeypatch):
    monkeypatch.setenv("OPS_API_KEY", API_KEY)
    with TestClient(create_app()) as c:
        r = c.get("/api/v1/game/tlk/1", headers=H(API_KEY))
        assert r.status_code == 503 and "GAME_NWN_ROOT" in r.json()["detail"]
        r = c.get("/api/v1/game/vault/X", headers=H(API_KEY))
        assert r.status_code == 503 and "SERVERVAULT" in r.json()["detail"]
        d = c.get("/api/v1/meta/systems", headers=H(API_KEY)).json()
        assert d["features"]["game_data"]["on"] is False
        routes = {(x["path"], x["access"]) for x in d["routes"]}
        assert ("/api/v1/game/vault/{folder}/{file}", "read") in routes
        assert ("/api/v1/game/vault/{folder}/{file}/retire", "admin") in routes


def test_tlk(client):
    assert client.get("/api/v1/game/tlk/1", headers=H(READ)).json() == {"strref": 1, "text": "Fighter"}
    assert client.get("/api/v1/game/tlk/16777217", headers=H(READ)).json()["text"] == "Rune Sight"
    assert client.get("/api/v1/game/tlk/-1", headers=H(READ)).status_code == 422
    assert client.get("/api/v1/game/tlk/1").status_code == 401


def test_2da(client):
    r = client.get("/api/v1/game/2da/feat", headers=H(READ)).json()
    assert r == {"table": "feat", "from": "tst_top", "columns": ["LABEL", "FEAT"], "rows": 3}
    r = client.get("/api/v1/game/2da/CLASSES/2?names=true", headers=H(READ)).json()
    assert r["columns"] == {"Label": "RuneKnight", "Name": "16777216", "StatGainTable": "cls_stat_rune"}
    assert r["text"] == {"Name": "Rune Knight"} and r["from"] == "base"
    assert "text" not in client.get("/api/v1/game/2da/classes/0", headers=H(READ)).json()
    assert client.get("/api/v1/game/2da/classes/9", headers=H(READ)).status_code == 404
    assert client.get("/api/v1/game/2da/nothing/0", headers=H(READ)).status_code == 404
    assert client.get("/api/v1/game/2da/nothing", headers=H(READ)).status_code == 404
    assert client.get("/api/v1/game/2da/bad.name/0", headers=H(READ)).status_code == 422


def test_item(client):
    r = client.get("/api/v1/game/items/tst_sword", headers=H(READ)).json()
    assert r["from"] == "tst_top" and r["name"] == "Hak Blade" and r["tag"] == "TST_SWORD"
    assert r["base_item"] == {"id": 0, "name": "Longsword"}
    assert r["examine"]["description"] == "A blade with a story." and r["examine"]["properties"] == ["Enhancement Bonus: +2"]
    assert r["examine"]["stats"][2].endswith(" 4015") and r["examine"]["level"] == {"required": 2, "tooLow": None}
    u = client.get("/api/v1/game/items/tst_sword?identified=false", headers=H(READ)).json()
    assert u["name"] == "Longsword (Unidentified)" and u["examine"]["properties"] == [] and u["examine"]["description"] == "A plain blade."
    assert client.get("/api/v1/game/items/tst_modring", headers=H(READ)).json()["from"] == "module"
    assert client.get("/api/v1/game/items/none_such", headers=H(READ)).status_code == 404
    assert client.get("/api/v1/game/items/a-b", headers=H(READ)).status_code == 422


def test_vault_and_character_sheet(client):
    ssh = fake_ssh()
    client.app.state.ssh = ssh
    bic = write_gff(character())

    async def run(cmd, timeout=15, input=None, errors="replace"):
        if cmd.startswith("stat -c %Y"):
            return SshResult(0, "1700000000\n" + base64.b64encode(bic).decode(), "")
        return await fake_ssh().run(cmd, timeout, input, errors)

    ssh.run = AsyncMock(side_effect=run)
    r = client.get("/api/v1/game/vault/ABCD1234", headers=H(READ))
    assert r.status_code == 200 and [c["file"] for c in r.json()["characters"]] == ["other", "testa"]
    r = client.get("/api/v1/game/vault/ABCD1234/testa", headers=H(READ)).json()
    assert r["folder"] == "ABCD1234" and r["file"] == "testa" and r["saved_at"] == "2023-11-14T22:13:20+00:00"
    assert r["name"]["full"] == "Testa Example" and r["classes"][1]["name"] == "Rune Knight" and len(r["equipped"]) == 2
    assert client.get("/api/v1/game/vault/NOPE/testa", headers=H(READ)).status_code == 404


def test_a_file_that_is_not_a_character_is_422(client):
    ssh = fake_ssh()
    client.app.state.ssh = ssh
    r = client.get("/api/v1/game/vault/ABCD1234/testa", headers=H(READ))      # fake_ssh's file is not GFF
    assert r.status_code == 422
    uti = write_gff({"__data_type": "UTI ", "Tag": {"type": "cexostring", "value": "x"}})

    async def run(cmd, timeout=15, input=None, errors="replace"):
        if cmd.startswith("stat -c %Y"):
            return SshResult(0, "1700000000\n" + base64.b64encode(uti).decode(), "")
        return await fake_ssh().run(cmd, timeout, input, errors)

    ssh.run = AsyncMock(side_effect=run)
    assert "not a character file" in client.get("/api/v1/game/vault/ABCD1234/testa", headers=H(READ)).json()["detail"]


def test_retire_needs_the_admin_key_and_nobody_online(client):
    client.app.state.ssh = fake_ssh()
    url = "/api/v1/game/vault/ABCD1234/testa/retire"
    assert client.post(url, headers=H(READ)).status_code == 403
    assert client.post(url, headers=H(TEAM)).status_code == 403
    r = client.post(url, headers=H(ADMIN))
    assert r.status_code == 200 and r.json()["dry_run"] is True
    with patch("app.routers.game.server.get_server_status", new=AsyncMock(return_value="running|Up")), \
            patch("app.routers.game.server.get_player_count", new=AsyncMock(return_value={"players": 1, "dms": 0})):
        assert client.post(url, headers=H(ADMIN), json={"dry_run": False}).status_code == 409
    with patch("app.routers.game.server.get_server_status", new=AsyncMock(return_value="running|Up")), \
            patch("app.routers.game.server.get_player_count", new=AsyncMock(return_value={"players": -1, "dms": -1})):
        assert client.post(url, headers=H(ADMIN), json={"dry_run": False}).status_code == 409
    with patch("app.routers.game.server.get_server_status", new=AsyncMock(return_value=None)):
        r = client.post(url, headers=H(ADMIN), json={"dry_run": False})
        assert r.status_code == 200 and r.json()["dry_run"] is False
    assert any("mv -n" in c for c in client.app.state.ssh.calls)
