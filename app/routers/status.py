import secrets
import shlex

from fastapi import APIRouter, HTTPException, Request

from app.auth import require_feature
from app.services import server

# Read routes: the read key opens them (main.py). The log stays on team_router: it names players and their addresses.
router = APIRouter(prefix="/api/v1/server", tags=["server"])
team_router = APIRouter(prefix="/api/v1/server", tags=["server"])


@router.get("/status", dependencies=[require_feature("game")])
async def server_status(request: Request):
    """The game server's state: running or not, when it last started, players and DMs, the module and NWNX image it runs, what is staged,
    a pending restart, changes waiting."""
    state = request.app.state
    return await server.get_full_status(
        ssh=state.ssh, db_pool=state.db_pool, redis_client=state.redis, http_session=state.http_session, settings=state.settings,
    )


@router.get("/online", dependencies=[require_feature("roster")])
async def server_online(request: Request):
    """Who is in the game now, from ONLINE_SQL: {"count", "characters": [{"name", ...}]}. 503 when the database doesn't answer."""
    state = request.app.state
    try:
        characters = await server.get_online(state.settings, state.db_pool)
    except server.RosterUnavailable:
        raise HTTPException(503, "The game database did not answer")
    return {"count": len(characters), "characters": characters}


@router.get("/modules", dependencies=[require_feature("deploy")])
async def server_modules(request: Request):
    """The modules on the game host that could be deployed (staged and live), with which one runs."""
    state = request.app.state
    return await server.get_available_modules(state.ssh, state.settings)


@team_router.get("/logs", dependencies=[require_feature("logs")])
async def server_logs(request: Request, lines: int = 20):
    """The last lines (at most 100) of the game's server log."""
    lines = max(1, min(lines, 100))
    state = request.app.state
    result = await state.ssh.run(f"tail -n {lines} {shlex.quote(state.settings.log_file)}", timeout=15)
    return {"lines": server.strip_ansi(result.stdout).splitlines()}


def _is_read_key(request: Request) -> bool:
    key = request.app.state.settings.ops_api_read_key
    token = request.headers.get("Authorization", "").removeprefix("Bearer ")
    return bool(key) and secrets.compare_digest(token, key)


@router.get("/progress", dependencies=[require_feature("game")])
async def server_progress(request: Request, lines: int = 60):
    """What the server is doing right now: running operations, the container, a countdown and the start-up markers in its log (with the
    log's last lines, except for the read key)."""
    state = request.app.state
    return await server.get_progress(state.ssh, state.redis, state.tasks, state.settings, lines, with_log=not _is_read_key(request))
