"""The whole app: it starts with only the key set, every part it lacks answers 503 naming its settings, and the key groups."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.routers import meta
from app.ssh import SshResult
from tests.conftest import API_KEY, make_settings

ADMIN, TEAM = "admin-key-0123456789", "team-key-0123456789"


def _h(key):
    return {"Authorization": f"Bearer {key}"}


def test_starts_with_only_the_key(monkeypatch):
    monkeypatch.setenv("OPS_API_KEY", API_KEY)
    with TestClient(create_app()) as c:
        assert c.get("/health").json() == {"status": "ok"}
        r = c.get("/api/v1/server/status", headers=_h(API_KEY))
        assert r.status_code == 503 and "GAME_HOST" in r.json()["detail"]
        r = c.post("/api/v1/server/release", headers=_h(API_KEY), json={"repo": "module"})
        assert r.status_code == 503 and "GITHUB_TOKEN and REPOS" in r.json()["detail"]
        d = c.get("/api/v1/meta/systems", headers=_h(API_KEY)).json()
        assert not any(f["on"] for f in d["features"].values())
        assert d["keys"] == {"full": True, "admin": False, "team": False}


def test_refuses_to_start_without_a_long_enough_key(monkeypatch):
    from pydantic import ValidationError
    monkeypatch.setenv("OPS_API_KEY", "short")
    with pytest.raises(ValidationError):
        create_app()


def test_refuses_a_bad_repos_setting(monkeypatch):
    from pydantic import ValidationError
    monkeypatch.setenv("OPS_API_KEY", API_KEY)
    monkeypatch.setenv("REPOS", "module=just-a-name")
    with pytest.raises(ValidationError):
        create_app()


@pytest.fixture
def client():
    s = make_settings(ops_api_admin_key=ADMIN, ops_api_team_key=TEAM, coolify_url="https://coolify.test", coolify_token="t",
                      coolify_service_uuid="u")
    with TestClient(create_app(s)) as c:
        yield c


def test_key_groups(client):
    ssh = client.app.state.ssh
    ssh.run = AsyncMock(return_value=SshResult(0, "running|Up 1 hour (healthy)", ""))
    with patch("app.routers.actions.restart_svc.restart_server", new=AsyncMock(return_value=(True, "ok"))):
        # The team key: restart, yes; stop, no.
        assert client.post("/api/v1/server/restart", headers=_h(TEAM), json={"wait_minutes": 1}).status_code == 200
        assert client.post("/api/v1/server/stop", headers=_h(TEAM), json={}).status_code == 403
        assert client.get("/api/v1/meta/systems", headers=_h(TEAM)).status_code == 403
    assert client.post("/api/v1/server/stop", headers=_h("wrong-key-0123456789"), json={}).status_code == 401
    assert client.post("/api/v1/server/stop", json={}).status_code == 401


def test_every_route_has_a_key_group(client):
    routes = meta.routes()
    assert {r["access"] for r in routes} == {"open", "team", "admin"}
    assert [r["path"] for r in routes if r["access"] == "open"] == ["/health"]
    by = {(r["path"], r["methods"][0]): r["access"] for r in routes}
    assert by[("/api/v1/server/restart", "POST")] == "team" and by[("/api/v1/server/deploy", "POST")] == "admin"
    assert by[("/api/v1/server/status", "GET")] == "team"


def test_a_second_restart_is_refused_while_one_runs(client):
    import asyncio
    gate = asyncio.Event()

    async def slow(**kw):
        await gate.wait()
        return True, "ok"
    with patch("app.routers.actions.restart_svc.restart_server", new=slow):
        assert client.post("/api/v1/server/restart", headers=_h(ADMIN), json={}).status_code == 200
        assert client.post("/api/v1/server/restart", headers=_h(ADMIN), json={}).status_code == 409


def test_deploy_refuses_a_module_name_that_is_not_plain(client):
    r = client.post("/api/v1/server/deploy", headers=_h(ADMIN), json={"module": "x/../y'"})
    assert r.status_code == 400


def test_start_when_running_does_nothing(client):
    client.app.state.ssh.run = AsyncMock(return_value=SshResult(0, "running|Up 1 hour (healthy)", ""))
    r = client.post("/api/v1/server/start", headers=_h(ADMIN))
    assert r.json() == {"ok": True, "message": "The server is already running."}


def test_start_when_stopped_asks_coolify(client):
    client.app.state.ssh.run = AsyncMock(return_value=SshResult(0, "exited|Exited (0) 1 minute ago", ""))
    client.app.state.coolify.post = AsyncMock(return_value={})
    r = client.post("/api/v1/server/start", headers=_h(ADMIN))
    assert r.status_code == 200
    client.app.state.coolify.post.assert_awaited_once_with("/services/u/restart")


def test_restart_without_redis_waits_then_restarts_from_outside(monkeypatch):
    s = make_settings(coolify_url="https://coolify.test", coolify_token="t", coolify_service_uuid="u")
    with TestClient(create_app(s)) as c:
        assert c.app.state.redis is None
        c.app.state.ssh.run = AsyncMock(return_value=SshResult(0, "running|Up 1 hour (healthy)", ""))
        sleeps = []

        async def fake_sleep(n):
            sleeps.append(n)
        hard = AsyncMock(return_value=(True, "Starting via Coolify."))
        with patch("app.routers.actions.asyncio.sleep", new=fake_sleep), \
             patch("app.routers.actions.restart_svc.hard_restart", new=hard), \
             patch("app.routers.actions.get_player_count", new=AsyncMock(return_value={"players": 0, "dms": 0})):
            assert c.post("/api/v1/server/restart", headers=_h(API_KEY), json={"wait_minutes": 2}).status_code == 200
            for _ in range(50):
                if hard.await_count:
                    break
                c.get("/health")
        assert sleeps == [120]
        hard.assert_awaited_once()


def test_stop_stops_even_when_the_game_does_not_answer():
    s = make_settings(redis_url="redis://:pw@h:6379")
    with TestClient(create_app(s)) as c:
        c.app.state.ssh.run = AsyncMock(return_value=SshResult(0, "running|Up 1 hour (healthy)", ""))
        stop = AsyncMock(return_value=(True, "Container stopped."))

        async def fake_sleep(n):
            pass
        with patch("app.routers.actions.redis_command", new=AsyncMock(return_value=(False, "No response from game server."))), \
             patch("app.routers.actions.asyncio.sleep", new=fake_sleep), \
             patch("app.routers.actions.restart_svc.stop_server", new=stop), \
             patch("app.routers.actions.get_player_count", new=AsyncMock(return_value={"players": 0, "dms": 0})):
            assert c.post("/api/v1/server/stop", headers=_h(API_KEY), json={"wait_minutes": 1}).status_code == 200
            for _ in range(50):
                if stop.await_count:
                    break
                c.get("/health")
        stop.assert_awaited_once()


def test_cancel_without_redis_cancels_the_waiting_restart():
    s = make_settings(coolify_url="https://coolify.test", coolify_token="t", coolify_service_uuid="u")
    with TestClient(create_app(s)) as c:
        c.app.state.ssh.run = AsyncMock(return_value=SshResult(0, "running|Up 1 hour (healthy)", ""))
        hard = AsyncMock(return_value=(True, "x"))
        with patch("app.routers.actions.restart_svc.hard_restart", new=hard), \
             patch("app.routers.actions.get_player_count", new=AsyncMock(return_value={"players": 0, "dms": 0})):
            c.post("/api/v1/server/restart", headers=_h(API_KEY), json={"wait_minutes": 5})
            r = c.post("/api/v1/server/cancel-restart", headers=_h(API_KEY))
            assert r.json() == {"ok": True, "message": "Restart cancelled"}
            assert c.post("/api/v1/server/cancel-restart", headers=_h(API_KEY)).json()["ok"] is False
        hard.assert_not_awaited()


def test_a_release_github_refuses_is_a_502_not_a_500():
    s = make_settings(github_token="bad", repos="module=me/my-module@main")
    refused = RuntimeError("401, message='Unauthorized'")  # what aiohttp's raise_for_status says
    with TestClient(create_app(s)) as c, \
            patch("app.routers.actions.release_svc.get_latest_tag", new=AsyncMock(side_effect=refused)):
        r = c.post("/api/v1/server/release", headers=_h(API_KEY), json={"repo": "module"})
    assert r.status_code == 502 and "Unauthorized" in r.json()["detail"]
