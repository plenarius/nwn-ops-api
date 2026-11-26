"""The app: `uvicorn app.main:create_app --factory`.

Start-up contacts nothing: the SSH connection, Redis and the database are opened on first use, so the API starts (and can bring the game
back) whatever else is down.
"""

import logging
from contextlib import asynccontextmanager

import aiohttp
from fastapi import FastAPI

from app import tracing
from app.auth import require_api_key
from app.config import Settings
from app.coolify import CoolifyClient
from app.db import create_db_pool
from app.redis_client import create_redis
from app.gamedata.data import GameData
from app.routers import actions, game, health, meta, server_repos, status
from app.ssh import SshClient
from app.task_registry import TaskRegistry

logger = logging.getLogger(__name__)


def log_to_stderr() -> None:
    """The app's own loggers (app.*) at INFO on stderr, so their lines reach the container's logs. uvicorn configures only its own loggers;
    the root logger is left alone so the libraries stay quiet."""
    log = logging.getLogger("app")
    if not log.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(levelname)s:     %(name)s: %(message)s"))
        log.addHandler(h)
    log.setLevel(logging.INFO)


def create_app(settings: Settings | None = None) -> FastAPI:
    log_to_stderr()
    # Request tracing (app/tracing.py): on only when an OTLP endpoint is set in the environment.
    tracing.setup()
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        s = app.state.settings
        app.state.ssh = SshClient(s.game_host, s.ssh_user, s.ssh_key_path, s.ssh_known_hosts) if s.game_host else None
        if s.game_host and not s.ssh_known_hosts:
            logger.warning("SSH_KNOWN_HOSTS is not set: the game host's key is not checked")
        app.state.redis = create_redis(s.redis_url)
        app.state.db_pool = await create_db_pool(s)
        app.state.http_session = aiohttp.ClientSession()
        app.state.coolify = (CoolifyClient(s.coolify_url.rstrip("/"), s.coolify_token, app.state.http_session)
                             if s.features()["coolify"] else None)
        app.state.tasks = TaskRegistry()
        # Read on first use, not here: start-up stays quick and contacts nothing.
        app.state.game_data = GameData(s) if s.features()["game_data"] else None
        on = [name for name, v in s.features().items() if v]
        logger.info("%s started; configured: %s", s.app_title, ", ".join(on) or "nothing but the keys")
        yield
        if app.state.ssh is not None:
            await app.state.ssh.close()
        if app.state.db_pool is not None:
            app.state.db_pool.close()
            await app.state.db_pool.wait_closed()
        if app.state.redis is not None:
            await app.state.redis.aclose()
        await app.state.http_session.close()
        tracing.shutdown()

    app = FastAPI(title=settings.app_title, lifespan=lifespan, telemetry=tracing.fastapi_telemetry())
    app.state.settings = settings

    full = settings.ops_api_key
    # The admin key: server operations (stop, start, deploy, NWNX, release) and the self-description, and nothing else.
    every = (full, settings.ops_api_admin_key, settings.ops_api_team_key, settings.ops_api_read_key)
    admin_deps = [require_api_key(full, settings.ops_api_admin_key, access="admin", known_keys=every)]
    # The team key: status, restart and cancelling it (a bot's or a GM page's key).
    team_deps = [require_api_key(full, settings.ops_api_admin_key, settings.ops_api_team_key, access="team", known_keys=every)]
    # The read key: what the server is doing, nothing that changes it or shows its log (a public status page's key).
    read_deps = [require_api_key(full, settings.ops_api_admin_key, settings.ops_api_team_key, settings.ops_api_read_key, access="read",
                                 known_keys=every)]

    meta.INCLUDED.clear()

    def include(router, dependencies=None):
        """app.include_router, noting the router and its key group for /api/v1/meta/systems."""
        meta.INCLUDED.append((router, dependencies or []))
        app.include_router(router, dependencies=dependencies)

    include(health.router)
    include(meta.router, dependencies=admin_deps)
    include(status.router, dependencies=read_deps)
    include(status.team_router, dependencies=team_deps)
    include(actions.team_router, dependencies=team_deps)
    include(actions.router, dependencies=admin_deps)
    include(server_repos.router, dependencies=admin_deps)
    include(game.router, dependencies=read_deps)
    include(game.admin_router, dependencies=admin_deps)
    return app
