"""The API describing itself, read from the running app so it never goes stale: every route with the key group that opens it (`access`,
set on each key's dependency in main.py) and the first line of its docstring, which parts are configured, which keys are set, the running
operations. Names only: no key, password or address."""

from __future__ import annotations

import inspect

from fastapi import APIRouter, Request
from fastapi.routing import APIRoute

from app import __version__

router = APIRouter(prefix="/api/v1/meta", tags=["meta"])

# The setting behind each key group: whether it is set shows which callers are switched on. (The full key opens every group.)
KEYS = {"full": "ops_api_key", "admin": "ops_api_admin_key", "team": "ops_api_team_key"}


def _access(route: APIRoute) -> str:
    for dep in route.dependant.dependencies:
        a = getattr(dep.call, "access", None)
        if a:
            return a
    return "open"


def _summary(fn) -> str:
    doc = inspect.getdoc(fn) or ""
    first = doc.split("\n\n", 1)[0].replace("\n", " ").strip()
    return first[:300]


# (router, the dependencies it was included with), noted by main.include: what routes() lists.
INCLUDED: list = []


def _deps_access(deps) -> str:
    for d in deps:
        a = getattr(getattr(d, "dependency", None), "access", None)
        if a:
            return a
    return ""


def routes() -> list[dict]:
    """Every route of the routers main.py included, with the key group its include gave it (or its own route dependencies), path and
    summary. FastAPI's app.routes is not used because its shape is internal."""
    out = []
    seen = set()
    for router, deps in INCLUDED:
        group_access = _deps_access(deps)
        for r in router.routes:
            if not isinstance(r, APIRoute):
                continue
            key = (r.path, tuple(sorted(r.methods)))
            if key in seen:
                continue
            seen.add(key)
            out.append({"path": r.path, "methods": sorted(m for m in r.methods if m != "HEAD"),
                        "access": group_access or _access(r), "group": (r.endpoint.__module__ or "").rsplit(".", 1)[-1],
                        "summary": _summary(r.endpoint)})
    return sorted(out, key=lambda x: (x["group"], x["path"]))


@router.get("/systems")
async def systems(request: Request):
    """What the API is and does right now: {version, routes, features, keys, operations}. Admin key."""
    state = request.app.state
    s = state.settings
    return {
        "version": __version__,
        "routes": routes(),
        "features": {name: {"on": on, "needs": s.NEEDS[name]} for name, on in s.features().items()},
        "keys": {group: bool(getattr(s, field, "")) for group, field in KEYS.items()},
        "operations": state.tasks.running_tasks(),
    }
