import logging
import shlex
from datetime import datetime, timezone

from app.redis_client import NO_REDIS, clear_restart_pending, redis_command, set_restart_pending
from app.services.server import get_server_status, is_running
from app.ssh import SshClient

logger = logging.getLogger(__name__)

NO_START = "No way to start the server is configured: set COOLIFY_API_URL, COOLIFY_TOKEN and COOLIFY_SERVICE_UUID, or GAME_START_COMMAND."


async def start_game(ssh: SshClient, coolify, settings) -> tuple[bool, str]:
    """Bring the game up with its current settings: a Coolify restart (which makes the containers again, so a new NWN_MODULE or image is
    used), or GAME_START_COMMAND over SSH when Coolify is not configured."""
    if coolify is not None and settings.features()["coolify"]:
        try:
            await coolify.post(f"/services/{settings.coolify_service_uuid}/restart")
            return True, "Starting via Coolify."
        except Exception as e:
            return False, f"Coolify restart failed: {e}"
    if settings.game_start_command:
        try:
            r = await ssh.run(settings.game_start_command + " 2>&1", timeout=180)
        except Exception as e:
            return False, f"Start command failed: {e}"
        if r.returncode == 0:
            return True, "Started with GAME_START_COMMAND."
        return False, f"Start command failed ({r.returncode}): {r.stdout[-500:]}"
    return False, NO_START


async def restart_server(redis_client, ssh: SshClient, coolify, settings, seconds: int, reason: str) -> tuple[bool, str]:
    """Ask the game for its countdown. If it doesn't answer and the server is not running, start it instead.

    Returns (False, NO_REDIS) when there is no Redis and the server runs: the caller restarts it from outside (hard_restart)."""
    ok, msg = await redis_command(redis_client, "restart", {"seconds": seconds, "reason": reason}, settings.redis_prefix,
                                  settings.redis_reply_timeout)

    if ok:
        await set_restart_pending(redis_client, reason, seconds, datetime.now(timezone.utc).isoformat(), settings.redis_prefix)
        return True, f"Restart countdown started ({seconds}s)"

    status = await get_server_status(ssh, settings.container_filter)
    if not is_running(status):
        started, smsg = await start_game(ssh, coolify, settings)
        return started, f"Server was stopped. {smsg}" if started else smsg

    return False, msg


async def stop_server(ssh: SshClient, name_filter: str = "nwserver") -> tuple[bool, str]:
    try:
        result = await ssh.run(
            f"ids=$(docker ps -q --filter name={shlex.quote(name_filter)}); [ -z \"$ids\" ] || docker stop $ids 2>&1",
            timeout=60,
        )
        if result.returncode == 0:
            return True, "Container stopped."
        return False, result.stderr or result.stdout
    except Exception as e:
        return False, str(e)


async def hard_restart(ssh: SshClient, coolify, settings) -> tuple[bool, str]:
    """Restart from outside, with no in-game countdown: stop the container, then start the game."""
    await stop_server(ssh, settings.container_filter)
    return await start_game(ssh, coolify, settings)


async def cancel_restart(redis_client, settings) -> tuple[bool, str]:
    ok, msg = await redis_command(redis_client, "cancel_restart", {}, settings.redis_prefix, settings.redis_reply_timeout)
    await clear_restart_pending(redis_client, settings.redis_prefix)
    if ok:
        return True, "Restart cancelled"
    return False, msg


__all__ = ["NO_REDIS", "NO_START", "start_game", "restart_server", "stop_server", "hard_restart", "cancel_restart"]
