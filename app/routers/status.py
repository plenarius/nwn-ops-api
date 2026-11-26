import shlex

from fastapi import APIRouter, Request

from app.auth import require_feature
from app.services import server

router = APIRouter(prefix="/api/v1/server", tags=["server"])


@router.get("/status", dependencies=[require_feature("game")])
async def server_status(request: Request):
    """The game server's state: running or not, players and DMs, the module and NWNX image it runs, what is staged, a pending restart,
    changes waiting."""
    state = request.app.state
    return await server.get_full_status(
        ssh=state.ssh, db_pool=state.db_pool, redis_client=state.redis, http_session=state.http_session, settings=state.settings,
    )


@router.get("/modules", dependencies=[require_feature("deploy")])
async def server_modules(request: Request):
    """The modules on the game host that could be deployed (staged and live), with which one runs."""
    state = request.app.state
    return await server.get_available_modules(state.ssh, state.settings)


@router.get("/logs", dependencies=[require_feature("logs")])
async def server_logs(request: Request, lines: int = 20):
    """The last lines (at most 100) of the game's server log."""
    lines = max(1, min(lines, 100))
    state = request.app.state
    result = await state.ssh.run(f"tail -n {lines} {shlex.quote(state.settings.log_file)}", timeout=15)
    return {"lines": server.strip_ansi(result.stdout).splitlines()}


@router.get("/progress", dependencies=[require_feature("game")])
async def server_progress(request: Request, lines: int = 60):
    """What the server is doing right now: running operations, the container, a countdown and the start-up markers in its log."""
    state = request.app.state
    return await server.get_progress(state.ssh, state.redis, state.tasks, state.settings, lines)
