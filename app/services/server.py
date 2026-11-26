import asyncio
import logging
import re
import shlex
from datetime import datetime, timezone

import aiohttp

from app import nwn_query
from app.redis_client import get_restart_pending
from app.services import nwnx_version
from app.ssh import SshClient

logger = logging.getLogger(__name__)


async def get_server_status(ssh: SshClient, name_filter: str = "nwserver") -> str | None:
    try:
        result = await ssh.run(
            f"docker ps -a --filter name={shlex.quote(name_filter)} "
            '--format "{{.State}}|{{.Status}}"'
        )
    except Exception:
        return None

    for line in result.stdout.splitlines():
        parts = line.split("|", 1)
        if len(parts) != 2:
            continue
        state, status_text = parts
        # "unhealthy" contains "healthy", so it has to be tested first; docker prints
        # "Up 2 hours (healthy)", "Up 2 hours (unhealthy)" and "Up 5 seconds (health: starting)".
        # ("Restarting (1) 5 seconds ago" also contains "starting", hence the specific match.)
        if "unhealthy" in status_text:
            return f"{state}:unhealthy"
        elif "health: starting" in status_text:
            return f"{state}:starting"
        elif "healthy" in status_text:
            return f"{state}:healthy"
        return state
    return None


def is_running(status: str | None) -> bool:
    return status is not None and "running" in status


async def get_player_count(settings, db_pool=None) -> dict:
    """Players and DMs online: from PLAYER_COUNT_SQL when the game database is configured, else from the game's own status query (which
    counts players only, so dms is 0). -1 when neither answers."""
    if db_pool is not None and settings.player_count_sql:
        try:
            async with db_pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(settings.player_count_sql)
                    row = await cur.fetchone()
                    return {"players": int(row[0] or 0), "dms": int(row[1] or 0) if len(row) > 1 else 0}
        except Exception as e:  # noqa: BLE001
            logger.warning("player count query failed: %s", type(e).__name__)
            return {"players": -1, "dms": -1}
    if not settings.game_host:
        return {"players": -1, "dms": -1}
    answer = await nwn_query.query(settings.game_host, settings.nwn_port)
    if answer is None:
        return {"players": -1, "dms": -1}
    return {"players": answer["players"], "dms": 0}


def anyone_on(counts: dict) -> bool:
    return counts["players"] > 0 or counts["dms"] > 0


async def get_module_name(ssh: SshClient, settings) -> str:
    """The module the server is set to run: NWN_MODULE in the env files (Coolify's first), else in the game container's environment."""
    files = " ".join(shlex.quote(f) for f in (settings.coolify_env_file, settings.nwn_env_file) if f)
    try:
        if files:
            result = await ssh.run(f"grep -h '^NWN_MODULE=' {files} 2>/dev/null | head -1 | cut -d= -f2-")
            if result.returncode == 0 and result.stdout.strip():
                # Coolify's copy quotes its values.
                return result.stdout.strip().strip('"\'')
        # Only the one line leaves the host: the container's other variables hold passwords.
        result = await ssh.run(
            f"docker inspect -f '{{{{range .Config.Env}}}}{{{{println .}}}}{{{{end}}}}' "
            f"$(docker ps -aq --filter name={shlex.quote(settings.container_filter)} | head -1) 2>/dev/null "
            "| grep '^NWN_MODULE=' | head -1 | cut -d= -f2-"
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return ""


async def get_module_version(ssh: SshClient, settings) -> str:
    name = await get_module_name(ssh, settings)
    return settings.module_version(name) if name else "?"


async def get_staged_files(ssh: SshClient, mod_stage: str, hak_stage: str) -> tuple[list[str], list[str]]:
    mod_files, hak_files = [], []
    if mod_stage:
        try:
            result = await ssh.run(f"ls {mod_stage}/ 2>/dev/null")
            if result.returncode == 0:
                mod_files = [f for f in result.stdout.splitlines() if f.strip()]
        except Exception:
            pass
    if hak_stage:
        try:
            result = await ssh.run(f"ls {hak_stage}/ 2>/dev/null")
            if result.returncode == 0:
                hak_files = [f for f in result.stdout.splitlines() if f.strip()]
        except Exception:
            pass
    return mod_files, hak_files


async def get_available_modules(ssh: SshClient, settings) -> list[dict]:
    mod_stage, mod_live = settings.mod_stage, settings.mod_live
    staged_names: set[str] = set()
    try:
        result = await ssh.run(f"ls {mod_stage}/*.mod 2>/dev/null")
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                fname = line.strip().rsplit("/", 1)[-1]
                if fname.endswith(".mod"):
                    staged_names.add(fname.removesuffix(".mod"))
    except Exception:
        pass

    current_mod = await get_module_name(ssh, settings)

    modules: list[dict] = []
    try:
        result = await ssh.run(f"ls -t {mod_live}/*.mod 2>/dev/null | head -20")
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                fname = line.strip().rsplit("/", 1)[-1]
                if fname.endswith(".mod"):
                    name = fname.removesuffix(".mod")
                    modules.append({"name": name, "file": fname, "staged": name in staged_names, "current": name == current_mod})
    except Exception:
        pass

    for sname in staged_names:
        if not any(m["name"] == sname for m in modules):
            modules.insert(0, {"name": sname, "file": f"{sname}.mod", "staged": True, "current": sname == current_mod})

    return modules[:5]


async def check_module_staged_differs(ssh: SshClient, mod_stage: str, mod_live: str) -> bool:
    if not (mod_stage and mod_live):
        return False
    try:
        result = await ssh.run(f"ls {mod_stage}/*.mod 2>/dev/null")
        if result.returncode != 0 or not result.stdout.strip():
            return False
        for line in result.stdout.splitlines():
            fname = line.strip().rsplit("/", 1)[-1]
            if not fname.endswith(".mod"):
                continue
            staged_md5, live_md5 = await asyncio.gather(
                ssh.run(f"md5sum {mod_stage}/{fname}"),
                ssh.run(f"md5sum {mod_live}/{fname} 2>/dev/null"),
            )
            if live_md5.returncode != 0:
                return True
            if staged_md5.stdout.split()[0] != live_md5.stdout.split()[0]:
                return True
    except Exception:
        logger.debug("check_module_staged_differs failed", exc_info=True)
    return False


async def _md5s(ssh: SshClient, paths: list[str]) -> dict[str, str]:
    """The md5 of each of these files that exists, by path, from one md5sum call (a missing file is left out)."""
    if not paths:
        return {}
    res = await ssh.run("md5sum " + " ".join(shlex.quote(p) for p in paths) + " 2>/dev/null", timeout=180)
    out = {}
    for line in res.stdout.splitlines():
        digest, _, path = line.partition("  ")
        if path:
            out[path] = digest
    return out


async def check_haks_staged_differs(ssh: SshClient, hak_stage: str, hak_live: str, tlk_live: str | None = None) -> bool:
    """Whether any staged hak (or talk table) differs from the live one: by size, and by md5 for files under 50 MB. A staged
    *.tlk is compared with the one in tlk_live (where it is promoted to), if given."""
    if not (hak_stage and hak_live):
        return False
    try:
        result = await ssh.run(
            f"ls -l {hak_stage}/ 2>/dev/null | awk 'NR>1 {{print $5, $NF}}'",
        )
        if result.returncode != 0 or not result.stdout.strip():
            return False
        staged_files = {}
        small_files = []
        for line in result.stdout.splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2:
                size = int(parts[0])
                fname = parts[1]
                staged_files[fname] = size
                if size < 50_000_000:
                    small_files.append(fname)
        if not staged_files:
            return False

        def live_dir(fname: str) -> str:
            return tlk_live if tlk_live and fname.endswith(".tlk") else hak_live

        live_sizes = {}
        folders = sorted({hak_live, tlk_live or hak_live})
        listings = await asyncio.gather(*(ssh.run(f"ls -l {folder}/ 2>/dev/null | awk 'NR>1 {{print $5, $NF}}'") for folder in folders))
        for folder, result in zip(folders, listings):
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    parts = line.split(None, 1)
                    if len(parts) == 2:
                        live_sizes[(folder, parts[1])] = int(parts[0])

        for fname, size in staged_files.items():
            if live_sizes.get((live_dir(fname), fname)) != size:
                return True

        if small_files:
            # Two md5sum calls in all (staged and live, side by side), not two per file one after another.
            staged, live = await asyncio.gather(
                _md5s(ssh, [f"{hak_stage}/{fname}" for fname in small_files]),
                _md5s(ssh, [f"{live_dir(fname)}/{fname}" for fname in small_files]),
            )
            for fname in small_files:
                live_md5 = live.get(f"{live_dir(fname)}/{fname}")
                if live_md5 is None:
                    return True
                staged_md5 = staged.get(f"{hak_stage}/{fname}")
                if staged_md5 is not None and staged_md5 != live_md5:
                    return True

        return False
    except Exception:
        logger.debug("check_haks_staged_differs failed", exc_info=True)
    return False


def _countdown(pending) -> dict | None:
    if not isinstance(pending, dict):
        return None
    try:
        at = datetime.fromisoformat(pending["started_at"]).timestamp() + pending.get("seconds", 0)
    except (KeyError, ValueError, TypeError):
        return None
    if at > datetime.now(timezone.utc).timestamp():
        return {"reason": pending.get("reason", "unknown"), "restart_at": datetime.fromtimestamp(at, tz=timezone.utc).isoformat()}
    return None


async def get_full_status(ssh: SshClient, db_pool, redis_client, http_session: aiohttp.ClientSession, settings) -> dict:
    s = settings
    (
        status, counts, mod_version, (staged_mods, staged_haks),
        nwnx_running, mod_differs, haks_differ, nwnx_latest,
    ) = await asyncio.gather(
        get_server_status(ssh, s.container_filter),
        get_player_count(s, db_pool),
        get_module_version(ssh, s),
        get_staged_files(ssh, s.mod_stage, s.hak_stage),
        nwnx_version.get_running(ssh, s.container_filter),
        check_module_staged_differs(ssh, s.mod_stage, s.mod_live),
        check_haks_staged_differs(ssh, s.hak_stage, s.hak_live, s.tlk_live),
        nwnx_version.get_latest(http_session, s.nwnx_image),
    )

    nwnx, pending = await asyncio.gather(
        nwnx_version.summary(http_session, nwnx_running, nwnx_latest, s.nwnx_repo, s.github_token),
        get_restart_pending(redis_client, s.redis_prefix),
        return_exceptions=True,
    )
    if isinstance(nwnx, BaseException):
        raise nwnx
    nwnx_update = nwnx["update_available"]

    changes_pending: list[str] = []
    if mod_differs:
        changes_pending.append("module")
    if haks_differ:
        changes_pending.append("haks")
    if nwnx_update:
        changes_pending.append("nwnx")

    return {
        "status": status,
        "players": counts["players"],
        "dms": counts["dms"],
        "module": mod_version,
        # Short commits of the running build and of the one an NWNX update would pull.
        "nwnx_running": nwnx["running"]["short"] or "unknown",
        "nwnx_latest": nwnx["latest"]["short"] or "?",
        "nwnx": nwnx,
        "staged_modules": staged_mods,
        "staged_haks": len(staged_haks),
        "restart_pending": None if isinstance(pending, BaseException) else _countdown(pending),
        "module_staged_differs": mod_differs,
        "haks_staged_differs": haks_differ,
        "nwnx_update_available": nwnx_update,
        "changes_pending": changes_pending,
    }


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


async def get_progress(ssh: SshClient, redis_client, tasks, settings, lines: int = 60) -> dict:
    """What the server is doing right now, read from the outside so it works for every kind of restart or deploy: the running operations,
    the container's state, a pending countdown, and the markers the game writes in its log as it starts (module named, startup, areas
    loaded)."""
    lines = max(1, min(lines, 200))
    log_file = shlex.quote(settings.log_file) if settings.log_file else ""

    async def nothing():
        return None
    container, log, markers, pending = await asyncio.gather(
        get_server_status(ssh, settings.container_filter),
        ssh.run(f"tail -n {lines} {log_file}", timeout=15) if log_file else nothing(),
        # The log is not cleared at a restart: take the latest "Loading Module" and only the markers after it.
        ssh.run(
            "awk '/Loading Module:/{m=$0;s=\"\";a=\"\"} /Primary server startup complete/{s=$0} /Area loading complete/{a=$0} "
            f"END{{print m; print s; print a}}' {log_file}", timeout=15,
        ) if log_file else nothing(),
        get_restart_pending(redis_client, settings.redis_prefix),
        return_exceptions=True,
    )
    out_lines = [] if log is None or isinstance(log, BaseException) else strip_ansi(log.stdout).splitlines()
    found = {"module": None, "startup": None, "areas": None}
    if markers is not None and not isinstance(markers, BaseException):
        for line in strip_ansi(markers.stdout).splitlines():
            if "Loading Module:" in line:
                found["module"] = line.strip()
            elif "Primary server startup complete" in line:
                found["startup"] = line.strip()
            elif "Area loading complete" in line:
                found["areas"] = line.strip()
    return {
        "now": datetime.now(timezone.utc).isoformat(),
        "operations": tasks.running_tasks(),
        "container": None if isinstance(container, BaseException) else container,
        "countdown": None if isinstance(pending, BaseException) else _countdown(pending),
        "markers": found,
        "log": out_lines,
    }
