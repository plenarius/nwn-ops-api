from types import SimpleNamespace

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from app.auth import require_api_key
from app.routers import meta
from app.task_registry import TaskRegistry
from tests.conftest import make_settings


def test_the_app_describes_itself(monkeypatch):
    app = FastAPI()
    team = [require_api_key("full", "t", access="team")]
    admin = [require_api_key("full", "a", access="admin")]
    things = APIRouter(prefix="/api/v1/things")

    @things.get("/{n}")
    async def thing(n: int):
        """One thing, as the team sees it.

        More words that are not the summary."""
        return {}
    health = APIRouter()

    @health.get("/health")
    async def ok():
        return {}
    monkeypatch.setattr(meta, "INCLUDED", [])
    for router, deps in ((things, team), (health, None), (meta.router, admin)):     # as main.include does
        meta.INCLUDED.append((router, deps or []))
        app.include_router(router, dependencies=deps)
    app.state.settings = make_settings(ops_api_team_key="team-key-0123456789", redis_url="redis://:pw@h:6379")
    app.state.tasks = TaskRegistry()
    c = TestClient(app)
    assert c.get("/api/v1/meta/systems").status_code == 401
    assert c.get("/api/v1/meta/systems", headers={"Authorization": "Bearer t"}).status_code == 401          # the team key is not enough
    d = c.get("/api/v1/meta/systems", headers={"Authorization": "Bearer a"}).json()
    by = {r["path"]: r for r in d["routes"]}
    assert by["/api/v1/things/{n}"]["access"] == "team" and by["/api/v1/things/{n}"]["summary"] == "One thing, as the team sees it."
    assert by["/health"]["access"] == "open" and by["/api/v1/meta/systems"]["access"] == "admin"
    assert d["keys"] == {"full": True, "admin": False, "team": True}
    assert d["features"]["redis"] == {"on": True, "needs": "REDIS_URL"} and d["features"]["coolify"]["on"] is False
    assert "pw" not in str(d) and "team-key" not in str(d)
