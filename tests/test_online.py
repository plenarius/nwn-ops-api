"""GET /api/v1/server/online: off without ONLINE_SQL, the read key opens it, rows by column name, 503 when the database doesn't answer."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services import server
from tests.conftest import API_KEY, make_settings

READ = "read-key-0123456789"
H = {"Authorization": f"Bearer {READ}"}


def pool(rows, columns=("name",), fail=False):
    cursor = AsyncMock()
    cursor.description = [(c,) for c in columns]
    cursor.fetchall = AsyncMock(return_value=rows)
    if fail:
        cursor.execute = AsyncMock(side_effect=OSError("gone"))
    cursor.__aenter__ = AsyncMock(return_value=cursor)
    cursor.__aexit__ = AsyncMock(return_value=False)
    conn = AsyncMock()
    conn.cursor = MagicMock(return_value=cursor)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    p = MagicMock()
    p.acquire = MagicMock(return_value=conn)
    p.wait_closed = AsyncMock()
    return p


@pytest.mark.asyncio
async def test_rows_by_column_name():
    s = make_settings(mysql_host="db", online_sql="SELECT name, area, level FROM online")
    got = await server.get_online(s, pool([("Mira", "The Docks", 7), ("Tam", None, 3)], ("name", "area", "level")))
    assert got == [{"name": "Mira", "area": "The Docks", "level": 7}, {"name": "Tam", "area": None, "level": 3}]


@pytest.mark.asyncio
async def test_capped():
    s = make_settings(mysql_host="db", online_sql="SELECT name FROM online")
    assert len(await server.get_online(s, pool([(f"c{i}",) for i in range(300)]))) == server.ROSTER_LIMIT


@pytest.mark.asyncio
async def test_database_down_is_not_nobody():
    s = make_settings(mysql_host="db", online_sql="SELECT name FROM online")
    with pytest.raises(server.RosterUnavailable):
        await server.get_online(s, pool([], fail=True))


def test_route(monkeypatch):
    s = make_settings(ops_api_read_key=READ, mysql_host="db", online_sql="SELECT name FROM online")
    with TestClient(create_app(s)) as c:
        c.app.state.db_pool = pool([("Mira",)])
        assert c.get("/api/v1/server/online").status_code == 401
        r = c.get("/api/v1/server/online", headers=H)
        assert r.status_code == 200 and r.json() == {"count": 1, "characters": [{"name": "Mira"}]}
        c.app.state.db_pool = pool([], fail=True)
        assert c.get("/api/v1/server/online", headers=H).status_code == 503
        routes = {(x["path"], x["access"]) for x in c.get("/api/v1/meta/systems", headers={"Authorization": f"Bearer {API_KEY}"}).json()["routes"]}
        assert ("/api/v1/server/online", "read") in routes


def test_off_without_online_sql():
    with TestClient(create_app(make_settings(ops_api_read_key=READ, mysql_host="db"))) as c:
        r = c.get("/api/v1/server/online", headers=H)
        assert r.status_code == 503 and "ONLINE_SQL" in r.json()["detail"]
