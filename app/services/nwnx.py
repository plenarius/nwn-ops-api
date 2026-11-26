import asyncio
import logging
import shlex
from datetime import datetime, timezone

from app import notify
from app.redis_client import redis_command, set_restart_pending
from app.services import nwnx_version
from app.services.restart import start_game, stop_server
from app.services.server import anyone_on, get_player_count, get_server_status, is_running
from app.ssh import SshClient

logger = logging.getLogger(__name__)


async def pull_image(ssh: SshClient, image: str) -> tuple[bool, str]:
    try:
        result = await ssh.run(f"docker pull {shlex.quote(image)}", timeout=300)
        if result.returncode != 0:
            return False, f"Pull failed: {(result.stderr or result.stdout)[-300:]}"
        pulled = "up to date" not in result.stdout.lower()
        if pulled:
            return True, "New image pulled"
        return False, "Image already up to date"
    except Exception as e:
        return False, f"Pull failed: {e}"


async def update_nwnx(ssh: SshClient, redis_client, coolify, http_session, wait_minutes: int, db_pool, settings) -> dict:
    s = settings
    # Whether this pull downloaded anything doesn't matter (a dashboard may have pulled already):
    # restart when the server runs an older build than the image now on the host.
    pulled, pull_msg = await pull_image(ssh, s.nwnx_image)

    status = await get_server_status(ssh, s.container_filter)
    if not is_running(status):
        return {
            "ok": True, "pulled": pulled,
            "message": "New image pulled. Server is stopped — use Restart to bring it up with the new image.",
        }

    running = await nwnx_version.get_running(ssh, s.container_filter)
    local = await nwnx_version.get_local_image(ssh, s.nwnx_image)
    if not nwnx_version.update_available(running, local):
        current = nwnx_version.short(running["revision"]) or "unknown"
        logger.info("nwnx update: nothing to do, running %s (%s)", current, pull_msg)
        return {"ok": True, "pulled": pulled, "message": f"Already running the latest NWNX build ({current})."}
    logger.info("nwnx update: %s -> %s", nwnx_version.short(running["revision"]), nwnx_version.short(local["revision"]))

    counts = await get_player_count(s, db_pool)
    if anyone_on(counts):
        seconds = max(wait_minutes, 1) * 60
        await redis_command(redis_client, "restart", {"seconds": seconds, "reason": "nwnx-update"}, s.redis_prefix, s.redis_reply_timeout)
        await set_restart_pending(redis_client, "nwnx-update", seconds, datetime.now(timezone.utc).isoformat(), s.redis_prefix)
        await notify.send(
            s, http_session, "nwnx.update",
            f"A new **NWNX** build has been pulled. {s.server_name} restarts in **{notify.minutes(seconds // 60)}**. Please save and log out.",
        )
        await asyncio.sleep(seconds + 15)

    await stop_server(ssh, s.container_filter)

    started, msg = await start_game(ssh, coolify, s)
    if not started:
        return {"ok": False, "pulled": pulled, "message": msg}

    await notify.send(s, http_session, "nwnx.updated",
                      f"{s.server_name} is restarting with the new **NWNX** build. It will be back shortly.")
    return {"ok": True, "pulled": pulled, "message": "NWNX updated, server restarting"}
