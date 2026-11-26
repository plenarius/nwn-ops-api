import asyncio
import logging
import shlex
from datetime import datetime, timezone

from app import notify
from app.redis_client import redis_command, set_restart_pending
from app.services import nwnx_version
from app.services.nwnx import pull_image
from app.services.restart import start_game, stop_server
from app.services.server import (
    anyone_on,
    check_haks_staged_differs,
    check_module_staged_differs,
    get_player_count,
    get_staged_files,
)
from app.ssh import SshClient

logger = logging.getLogger(__name__)


def promote_haks_command(settings) -> str:
    """The shell command that puts the staged haks live: each staged file moves to the hak folder, except a talk table (*.tlk), which goes
    to the folder the game server reads its custom TLK from (TLK_LIVE), if set."""
    tlk = settings.tlk_live or settings.hak_live
    sudo = settings.sudo
    return (f"cd {settings.hak_stage} && for f in *; do [ -f \"$f\" ] || continue; case \"$f\" in "
            f"*.tlk) {sudo}mv -f -- \"$f\" {tlk}/ ;; *) {sudo}mv -f -- \"$f\" {settings.hak_live}/ ;; esac || exit 1; done 2>&1")


async def _set_module(ssh: SshClient, coolify, settings, mod_name: str) -> None:
    """Point the server at the module: NWN_MODULE in the env files on the game host and in Coolify's settings for the service."""
    files = " ".join(shlex.quote(f) for f in (settings.nwn_env_file, settings.coolify_env_file) if f)
    if files:
        r = await ssh.run(f"{settings.sudo}sed -i 's/^NWN_MODULE=.*/NWN_MODULE={mod_name}/' {files} 2>&1", timeout=10)
        if r.returncode != 0:
            raise RuntimeError(f"sed: {r.stdout[-300:]}")
    if coolify is not None and settings.features()["coolify"]:
        await coolify.patch_env(settings.coolify_service_uuid, "NWN_MODULE", mod_name)


async def _hook(ssh: SshClient, settings, mod_name: str, errors: list[str]) -> None:
    if not settings.deploy_hook:
        return
    try:
        r = await ssh.run(f"{settings.sudo}env MODULE={shlex.quote(mod_name)} {settings.deploy_hook} 2>&1", timeout=600)
        if r.returncode != 0:
            errors.append(f"Deploy hook ({r.returncode}): {r.stdout[-300:]}")
    except Exception as e:
        errors.append(f"Deploy hook: {e}")


async def deploy_module(ssh: SshClient, redis_client, coolify, http_session, module: dict, wait_minutes: int, requester: str,
                        settings) -> list[str]:
    """Execute a full deploy. Returns list of errors (empty = success)."""
    s = settings
    mod_name = module["name"]
    version = s.module_version(mod_name)
    errors = []
    staged = bool(module.get("staged"))

    if wait_minutes > 0:
        seconds = wait_minutes * 60
        reason = f"deploy {s.module_label(mod_name)} by {requester}"
        ok, _ = await redis_command(redis_client, "restart", {"seconds": seconds, "reason": reason}, s.redis_prefix, s.redis_reply_timeout)
        if ok:
            await set_restart_pending(redis_client, reason, seconds, datetime.now(timezone.utc).isoformat(), s.redis_prefix)
        await notify.send(
            s, http_session, "server.update_incoming",
            f"{s.server_name} restarts in **{notify.minutes(wait_minutes)}** to apply updates. Please save and log out.",
            version=version,
        )
        # A little extra so the in-game countdown's own save and shutdown finish first.
        await asyncio.sleep(seconds + 15)

    # Stop the container before swapping files, and bring it back with a Coolify restart:
    # that recreates the container, so it picks up the new NWN_MODULE. An in-game restart
    # only restarts the same container, which keeps the old module name in its environment.
    await stop_server(ssh, s.container_filter)

    promoted = True
    if staged:
        try:
            result = await ssh.run(f"{s.sudo}sh -c 'mv {s.mod_stage}/*.mod {s.mod_live}/' 2>&1", timeout=30)
            if result.returncode != 0:
                promoted = False
                errors.append(f"Stage promote: {result.stderr or result.stdout}")
        except Exception as e:
            promoted = False
            errors.append(f"Stage promote: {e}")

    # Only point the server at the new module if its file is in place.
    if promoted:
        try:
            await _set_module(ssh, coolify, s, mod_name)
        except Exception as e:
            promoted = False
            errors.append(f"Set NWN_MODULE: {e}")

    # Haks go live only with their module: they can depend on each other (2da rows).
    _, hak_files = await get_staged_files(ssh, s.mod_stage, s.hak_stage)
    if hak_files and promoted and s.hak_live:
        try:
            result = await ssh.run(promote_haks_command(s), timeout=60)
            if result.returncode != 0:
                errors.append(f"Hak move: {result.stderr or result.stdout}")
        except Exception as e:
            errors.append(f"Hak move: {e}")

    if promoted:
        await _hook(ssh, s, mod_name, errors)

    if not errors and s.development_dir:
        await ssh.run(f"{s.sudo}sh -c 'rm -f {s.development_dir}/*' 2>/dev/null")

    # The server is stopped: always bring it back, even after an error above.
    started, msg = await start_game(ssh, coolify, s)
    if not started:
        errors.append(msg)
        return errors

    if errors:
        return errors

    await notify.send(s, http_session, "server.restarting",
                      f"{s.server_name} is restarting now with **{s.module_label(mod_name)}**. It will be back shortly.", version=version)
    return []


async def _newest_staged_module(ssh: SshClient, settings) -> str:
    """The name (without .mod) of the newest staged module, or "" if none."""
    ls_result = await ssh.run(f"ls -t {settings.mod_stage}/*.mod 2>/dev/null")
    if ls_result.returncode == 0 and ls_result.stdout.strip():
        newest = ls_result.stdout.splitlines()[0].strip()
        return newest.rsplit("/", 1)[-1].removesuffix(".mod")
    return ""


async def full_deploy(ssh: SshClient, redis_client, coolify, http_session, wait_minutes: int, settings, db_pool,
                      skip: list[str] | None = None) -> dict:
    s = settings
    skip = skip or []

    mod_differs, haks_differ, nwnx_running, nwnx_latest = await asyncio.gather(
        check_module_staged_differs(ssh, s.mod_stage, s.mod_live),
        check_haks_staged_differs(ssh, s.hak_stage, s.hak_live, s.tlk_live),
        nwnx_version.get_running(ssh, s.container_filter),
        nwnx_version.get_latest(http_session, s.nwnx_image),
    )
    nwnx_update = nwnx_version.update_available(nwnx_running, nwnx_latest)
    running_rev, latest_rev = nwnx_running.get("revision"), nwnx_latest.get("revision")
    if not (running_rev and latest_rev):
        logger.warning("full_deploy: NWNX not checked, running %s, latest %s",
                       nwnx_version.short(running_rev) or "unknown", nwnx_version.short(latest_rev) or "unknown")

    to_apply: list[str] = []
    if mod_differs and "module" not in skip:
        to_apply.append("module")
    if haks_differ and "haks" not in skip:
        to_apply.append("haks")
    if nwnx_update and "nwnx" not in skip:
        to_apply.append("nwnx")

    if not to_apply:
        return {"ok": True, "applied": [], "message": "Nothing to deploy"}

    mod_name = ""
    if "module" in to_apply:
        mod_name = await _newest_staged_module(ssh, s)
        if not mod_name:
            return {"ok": False, "applied": [], "message": "No staged module found; nothing was deployed"}

    counts = await get_player_count(s, db_pool)
    if anyone_on(counts) and wait_minutes > 0:
        seconds = wait_minutes * 60
        await redis_command(redis_client, "restart", {"seconds": seconds, "reason": "full-deploy"}, s.redis_prefix, s.redis_reply_timeout)
        await set_restart_pending(redis_client, "full-deploy", seconds, datetime.now(timezone.utc).isoformat(), s.redis_prefix)
        await notify.send(
            s, http_session, "server.update_incoming",
            f"{s.server_name} restarts in **{notify.minutes(wait_minutes)}** to apply updates. Please save and log out.",
        )
        await asyncio.sleep(seconds)

    await stop_server(ssh, s.container_filter)

    errors: list[str] = []

    if "nwnx" in to_apply:
        pulled, pull_msg = await pull_image(ssh, s.nwnx_image)
        if not pulled:
            errors.append(f"NWNX pull: {pull_msg}")

    if "module" in to_apply:
        module_in = False
        try:
            result = await ssh.run(f"{s.sudo}sh -c 'mv {s.mod_stage}/*.mod {s.mod_live}/' 2>&1", timeout=30)
            if result.returncode != 0:
                errors.append(f"Module move: {result.stderr or result.stdout}")
            else:
                await _set_module(ssh, coolify, s, mod_name)
                module_in = True
        except Exception as e:
            errors.append(f"Module promote: {e}")
        if not module_in:
            # The old module keeps running, so its haks stay too.
            to_apply = [t for t in to_apply if t not in ("module", "haks")]

    # Haks go live only with their module (2da rows), so after it.
    if "haks" in to_apply:
        try:
            result = await ssh.run(promote_haks_command(s), timeout=60)
            if result.returncode != 0:
                errors.append(f"Hak move: {result.stderr or result.stdout}")
        except Exception as e:
            errors.append(f"Hak move: {e}")

    if "module" in to_apply or "haks" in to_apply:
        await _hook(ssh, s, mod_name, errors)

    started, start_msg = await start_game(ssh, coolify, s)
    if not started:
        errors.append(start_msg)

    summary = ", ".join(to_apply)
    if errors:
        msg = f"Deployed [{summary}] with errors: {'; '.join(errors)}"
        logger.error("full_deploy errors: %s", errors)
    else:
        msg = f"Deployed [{summary}] successfully"

    applied = ", ".join(f"**{t}**" for t in to_apply)
    await notify.send(
        s, http_session, "server.updated",
        (f"{s.server_name} is restarting with updates: {applied}. " if applied else f"{s.server_name} is restarting. ")
        + "It will be back shortly.",
    )

    return {"ok": not errors, "applied": to_apply, "message": msg}
