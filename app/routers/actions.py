import asyncio
import logging
import re

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app import notify
from app.auth import require_feature
from app.services import deploy as deploy_svc
from app.services import nwnx as nwnx_svc
from app.services import release as release_svc
from app.services import repo_overview
from app.services import restart as restart_svc
from app.services.server import anyone_on, get_available_modules, get_module_version, get_player_count, get_server_status, is_running

logger = logging.getLogger(__name__)

# Admin-only operations (stop, deploy, NWNX, release) and the ones a team may also use (restart and cancelling it) are separate routers so
# main.py can give them different keys.
router = APIRouter(prefix="/api/v1/server", tags=["actions"])
team_router = APIRouter(prefix="/api/v1/server", tags=["actions"])

# A module name that can go into a file name and a sed expression as it is.
MODULE_NAME = re.compile(r"[\w .-]{1,64}")


class RestartRequest(BaseModel):
    wait_minutes: int = 5
    reason: str = "manual"


class StopRequest(BaseModel):
    wait_minutes: int = 5
    reason: str = "maintenance"


class NwnxUpdateRequest(BaseModel):
    wait_minutes: int = 5


class DeployRequest(BaseModel):
    module: str
    wait_minutes: int = 10


class FullDeployRequest(BaseModel):
    wait_minutes: int = 5
    skip: list[str] = []


class ReleaseRequest(BaseModel):
    repo: str
    bump: str = "minor"
    tag_override: str | None = None


def _seconds(wait_minutes: int) -> int:
    seconds = max(0, min(wait_minutes, 30)) * 60
    return seconds or 60


def _version_or_none(mod: str) -> str | None:
    return mod.strip() if mod.strip() not in ("", "?") else None


@team_router.post("/restart", dependencies=[require_feature("game")])
async def restart(body: RestartRequest, request: Request):
    """Restart the game server after a countdown (at most 30 minutes), announcing it on Discord if anyone is on.

    With Redis the game runs the countdown and shuts itself down; without it, the API waits the countdown and restarts the container."""
    state = request.app.state
    tasks = state.tasks
    s = state.settings

    if tasks.is_running("restart") or tasks.is_running("deploy"):
        raise HTTPException(409, "A restart or deploy is already in progress")

    seconds = _seconds(body.wait_minutes)

    async def _announce(kind: str, text: str):
        counts = await get_player_count(s, state.db_pool)
        if anyone_on(counts):
            mod = await get_module_version(state.ssh, s)
            await notify.send(s, state.http_session, kind, text, version=_version_or_none(mod))

    async def _do_restart():
        ok, msg = await restart_svc.restart_server(
            redis_client=state.redis, ssh=state.ssh, coolify=state.coolify, settings=s, seconds=seconds, reason=body.reason,
        )
        logger.info("restart: %s", msg)
        text = f"{s.server_name} restarts in **{notify.minutes(seconds // 60)}**. Please save and log out."
        if ok:
            await _announce("server.restart_scheduled", text)
        elif msg == restart_svc.NO_REDIS:
            await _announce("server.restart_scheduled", text)
            await asyncio.sleep(seconds)
            ok, msg = await restart_svc.hard_restart(state.ssh, state.coolify, s)
            logger.info("restart without Redis: %s", msg)

    tasks.start("restart", _do_restart())
    return {"ok": True, "message": f"Restart countdown started ({seconds}s)"}


@router.post("/stop", dependencies=[require_feature("game")])
async def stop(body: StopRequest, request: Request):
    """Stop the game server after a countdown (at most 30 minutes), announcing it on Discord if anyone is on."""
    state = request.app.state
    tasks = state.tasks
    s = state.settings

    if tasks.is_running("stop") or tasks.is_running("deploy"):
        raise HTTPException(409, "A stop or deploy is already in progress")

    if not is_running(await get_server_status(state.ssh, s.container_filter)):
        return {"ok": True, "message": "The server is not running."}

    seconds = _seconds(body.wait_minutes)

    async def _do_stop():
        ok, msg = await restart_svc.restart_server(
            redis_client=state.redis, ssh=state.ssh, coolify=state.coolify, settings=s, seconds=seconds, reason=body.reason,
        )
        if ok or msg == restart_svc.NO_REDIS:
            counts = await get_player_count(s, state.db_pool)
            if anyone_on(counts):
                await notify.send(s, state.http_session, "server.stopping",
                                  f"{s.server_name} shuts down in **{notify.minutes(seconds // 60)}**. Please save and log out.")
            await asyncio.sleep(seconds + 15)
            ok, msg = await restart_svc.stop_server(state.ssh, s.container_filter)
        logger.info("stop: %s", msg)

    tasks.start("stop", _do_stop())
    return {"ok": True, "message": f"Stop countdown started ({seconds}s)"}


@router.post("/start", dependencies=[require_feature("game")])
async def start(request: Request):
    """Start the game server if it is not running (Coolify, or GAME_START_COMMAND)."""
    state = request.app.state
    s = state.settings
    if state.tasks.running_tasks():
        raise HTTPException(409, f"Busy: {', '.join(state.tasks.running_tasks())}")
    if is_running(await get_server_status(state.ssh, s.container_filter)):
        return {"ok": True, "message": "The server is already running."}
    ok, msg = await restart_svc.start_game(state.ssh, state.coolify, s)
    if not ok:
        raise HTTPException(503 if msg == restart_svc.NO_START else 502, msg)
    return {"ok": True, "message": msg}


@team_router.post("/cancel-restart", dependencies=[require_feature("game")])
async def cancel_restart(request: Request):
    """Cancel a pending restart countdown, and say so on Discord."""
    state = request.app.state
    s = state.settings
    if state.redis is None:
        # No game countdown: the countdown is the API's own wait.
        ok = state.tasks.cancel("restart") | state.tasks.cancel("stop")
        msg = "Restart cancelled" if ok else "No restart is in progress."
    else:
        ok, msg = await restart_svc.cancel_restart(state.redis, s)
    if ok:
        await notify.send(s, state.http_session, "server.restart_cancelled", "The scheduled restart has been cancelled.")
    return {"ok": ok, "message": msg}


@router.post("/deploy", dependencies=[require_feature("deploy")])
async def deploy(body: DeployRequest, request: Request):
    """Deploy a staged (or live) module to the server after a countdown, in the background."""
    state = request.app.state
    tasks = state.tasks
    s = state.settings

    if tasks.is_running("deploy"):
        raise HTTPException(409, "A deploy is already in progress")
    if not MODULE_NAME.fullmatch(body.module):
        raise HTTPException(400, "A module name is letters, digits, spaces, '.', '_' and '-'")

    modules = await get_available_modules(state.ssh, s)
    module = next((m for m in modules if m["name"] == body.module), None)
    if not module:
        raise HTTPException(404, f"Module '{body.module}' not found")

    async def _do_deploy():
        errors = await deploy_svc.deploy_module(
            ssh=state.ssh, redis_client=state.redis, coolify=state.coolify, http_session=state.http_session, module=module,
            wait_minutes=max(0, min(body.wait_minutes, 30)), requester="api", settings=s,
        )
        if errors:
            logger.error("Deploy errors: %s", errors)
        else:
            logger.info("Deploy of %s done", body.module)

    tasks.start("deploy", _do_deploy())
    return {"ok": True, "message": f"Deploy started for v{s.module_version(body.module)}"}


@router.post("/full-deploy", dependencies=[require_feature("deploy")])
async def full_deploy(body: FullDeployRequest, request: Request):
    """Put on the server whatever staged differs from it (module, haks, NWNX image) after one countdown, in the background."""
    state = request.app.state
    tasks = state.tasks

    for t in ("deploy", "full_deploy", "nwnx_update", "restart", "stop"):
        if tasks.is_running(t):
            raise HTTPException(409, f"Conflict: {t} is already in progress")

    async def _do_full_deploy():
        result = await deploy_svc.full_deploy(
            ssh=state.ssh, redis_client=state.redis, coolify=state.coolify, http_session=state.http_session,
            wait_minutes=max(0, min(body.wait_minutes, 30)), settings=state.settings, db_pool=state.db_pool, skip=body.skip,
        )
        (logger.info if result["ok"] else logger.error)("full_deploy: %s", result["message"])

    tasks.start("full_deploy", _do_full_deploy())
    return {"ok": True, "message": "Full deploy started"}


@router.post("/nwnx/pull", dependencies=[require_feature("game")])
async def nwnx_pull(request: Request):
    """Pull the newest NWNX image on the game host without restarting."""
    state = request.app.state
    pulled, msg = await nwnx_svc.pull_image(state.ssh, state.settings.nwnx_image)
    return {"ok": True, "pulled": pulled, "message": msg}


@router.post("/nwnx/update", dependencies=[require_feature("game")])
async def nwnx_update(body: NwnxUpdateRequest, request: Request):
    """Move the server to the newest NWNX image after a countdown, in the background."""
    state = request.app.state
    tasks = state.tasks

    if tasks.is_running("nwnx_update"):
        raise HTTPException(409, "An NWNX update is already in progress")

    async def _do_update():
        result = await nwnx_svc.update_nwnx(
            ssh=state.ssh, redis_client=state.redis, coolify=state.coolify, http_session=state.http_session,
            wait_minutes=max(0, min(body.wait_minutes, 30)), db_pool=state.db_pool, settings=state.settings,
        )
        logger.info("nwnx update: %s", result["message"])

    tasks.start("nwnx_update", _do_update())
    return {"ok": True, "message": "NWNX update started"}


def _repo(s, key: str):
    repo = s.repo_map.get(key)
    if repo is None:
        raise HTTPException(400, f"Unknown repo: {key} (REPOS has {', '.join(s.repo_map) or 'none'})")
    return repo


@router.get("/release/preview", dependencies=[require_feature("github")])
async def release_preview(request: Request, repo: str | None = None):
    """Per repository: the current tag, the next ones, and the notes GitHub would write for the next minor release."""
    state = request.app.state
    s = state.settings
    repos = [_repo(s, repo)] if repo else list(s.repo_map.values())

    async def one(r):
        try:
            major, minor, current = await release_svc.get_latest_tag(state.http_session, r.full_name, s.github_token, s.release_tag_regex)
            nxt = release_svc.next_tags(major, minor, s.release_tag_format)
            notes = await release_svc.generate_notes(state.http_session, s.github_token, r, nxt["next_minor"], current)
            return {"repo": r.key, "current": current or None, **nxt, "notes": notes}
        except Exception as e:
            return {"repo": r.key, "current": "?", "next_minor": "?", "next_major": "?", "notes": None, "error": str(e)}

    return {"repos": list(await asyncio.gather(*(one(r) for r in repos)))}


@router.post("/release", dependencies=[require_feature("github")])
async def create_release(body: ReleaseRequest, request: Request):
    """Tag a release of a repository in REPOS on its branch, with GitHub's generated notes."""
    state = request.app.state
    s = state.settings
    repo = _repo(s, body.repo)

    if body.tag_override:
        tag = body.tag_override
    else:
        major, minor, _ = await release_svc.get_latest_tag(state.http_session, repo.full_name, s.github_token, s.release_tag_regex)
        tags = release_svc.next_tags(major, minor, s.release_tag_format)
        tag = tags["next_major"] if body.bump == "major" else tags["next_minor"]

    name = s.release_name.format(tag=tag, repo=repo.key)
    result = await release_svc.create_release(state.http_session, s.github_token, repo, tag, name)
    if not result["ok"]:
        raise HTTPException(502, result["message"])
    # The overview should show the new release on its next look, not after the cache runs out.
    repo_overview.forget()
    return {"ok": True, "tag": result["tag"], "url": result["url"], "message": f"Release {result['tag']} created for {repo.key}"}
