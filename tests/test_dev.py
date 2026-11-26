"""/api/v1/dev: off without DEV_REPO, load dispatches the workflow once and refuses while a run is going, status reads the status file."""

import json

from fastapi.testclient import TestClient

from app.main import create_app
from app.services import dev
from tests.conftest import API_KEY, make_settings

TEAM, READ = "team-key-0123456789", "read-key-0123456789"


def app_with(tmp_path, runs, calls, **kw):
    s = make_settings(ops_api_team_key=TEAM, ops_api_read_key=READ, github_token="gh", dev_repo="you/mymodule",
                      dev_status_file=str(tmp_path / "status.json"), **kw)

    async def fake_gh(session, token, method, url, body=None):
        calls.append((method, url, body))
        return {"workflow_runs": runs} if method == "GET" else None

    return s, fake_gh


def run(status, conclusion=None):
    return {"id": 7, "display_title": "dev load pr-12", "status": status, "conclusion": conclusion,
            "created_at": "2026-11-26T20:00:00Z", "html_url": "https://github.com/you/mymodule/actions/runs/7"}


def test_off_without_settings():
    with TestClient(create_app(make_settings())) as c:
        r = c.post("/api/v1/dev/load", json={"target": "main"}, headers={"Authorization": f"Bearer {API_KEY}"})
        assert r.status_code == 503 and "DEV_REPO" in r.json()["detail"]


def test_load_dispatches(tmp_path, monkeypatch):
    calls = []
    s, gh = app_with(tmp_path, [run("completed", "success")], calls)
    monkeypatch.setattr(dev, "_gh", gh)
    with TestClient(create_app(s)) as c:
        assert c.post("/api/v1/dev/load", json={"target": "pr-12"}, headers={"Authorization": f"Bearer {READ}"}).status_code == 403
        r = c.post("/api/v1/dev/load", json={"target": "pr-12", "requested_by": "Mira"}, headers={"Authorization": f"Bearer {TEAM}"})
        assert r.status_code == 200 and r.json()["target"] == "pr-12"
    method, url, body = calls[-1]
    assert method == "POST" and url.endswith("/repos/you/mymodule/actions/workflows/dev-load.yml/dispatches")
    assert body == {"ref": "main", "inputs": {"target": "pr-12", "requested_by": "Mira"}}


def test_load_refuses_while_running_and_bad_targets(tmp_path, monkeypatch):
    calls = []
    s, gh = app_with(tmp_path, [run("in_progress")], calls)
    monkeypatch.setattr(dev, "_gh", gh)
    with TestClient(create_app(s)) as c:
        h = {"Authorization": f"Bearer {TEAM}"}
        r = c.post("/api/v1/dev/load", json={"target": "main"}, headers=h)
        assert r.status_code == 409 and "already running" in r.json()["detail"]
        for bad in ("pr-0", "pr-12; rm -rf", "ee", "pr-"):
            assert c.post("/api/v1/dev/load", json={"target": bad}, headers=h).status_code == 422
    assert not [x for x in calls if x[0] == "POST"]


def test_status(tmp_path, monkeypatch):
    calls = []
    s, gh = app_with(tmp_path, [run("completed", "success")], calls)
    monkeypatch.setattr(dev, "_gh", gh)
    with TestClient(create_app(s)) as c:
        h = {"Authorization": f"Bearer {READ}"}
        assert c.get("/api/v1/dev/status", headers=h).json()["loaded"] is None
        (tmp_path / "status.json").write_text(json.dumps({"label": "pr-12-3f9c2ab", "commit": "3f9c2ab", "content": "ab" * 20,
                                                          "dev_haks": ["newhak"], "health": "healthy", "secret": "x"}))
        got = c.get("/api/v1/dev/status", headers=h).json()
    assert got["loaded"]["label"] == "pr-12-3f9c2ab" and got["loaded"]["dev_haks"] == ["newhak"] and "secret" not in got["loaded"]
    assert got["last_run"]["conclusion"] == "success"
