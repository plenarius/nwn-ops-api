"""The dev server (the guide's chapter 14): start the workflow that loads a build onto it, and say what it runs.

The API never reaches the dev server itself. A load is a workflow_dispatch of DEV_WORKFLOW in DEV_REPO, which fetches the compile check's
module and delivers it through the dev server's forced-command key. What is loaded comes from DEV_STATUS_FILE, the status.json the dev
server's dev.sh writes after every load, mounted read-only into this container."""

import json
import logging
import re
from datetime import datetime, timezone

import aiohttp

logger = logging.getLogger(__name__)

API = "https://api.github.com"
# What a load may name: a pull request, main, or the build before.
TARGET = re.compile(r"^(pr-[1-9][0-9]{0,6}|main|rollback)$")


class DevError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


async def _gh(session: aiohttp.ClientSession, token: str, method: str, url: str, body: dict | None = None):
    async with session.request(method, url, json=body, headers=_headers(token), timeout=aiohttp.ClientTimeout(total=15)) as r:
        if r.status >= 400:
            text = (await r.text())[:200]
            # A refused token or a missing workflow is GitHub's answer, not ours: 502 with what it said.
            raise DevError(502, f"GitHub answered {r.status}: {text}")
        return await r.json(content_type=None) if r.status != 204 else None


def _run(r: dict) -> dict:
    return {"id": r["id"], "title": r.get("display_title", ""), "status": r["status"], "conclusion": r.get("conclusion"),
            "created_at": r["created_at"], "url": r["html_url"]}


async def last_run(session, settings) -> dict | None:
    url = f"{API}/repos/{settings.dev_repo}/actions/workflows/{settings.dev_workflow}/runs?per_page=1"
    data = await _gh(session, settings.github_token, "GET", url)
    runs = (data or {}).get("workflow_runs") or []
    return _run(runs[0]) if runs else None


async def load(session, settings, target: str, who: str) -> dict:
    if not TARGET.match(target):
        raise DevError(422, "target is pr-<number>, main or rollback")
    run = await last_run(session, settings)
    if run and run["status"] != "completed":
        # One load at a time: the dev server is one game, and two loads would race for it.
        raise DevError(409, f"a dev load is already running: {run['title']} ({run['url']})")
    url = f"{API}/repos/{settings.dev_repo}/actions/workflows/{settings.dev_workflow}/dispatches"
    await _gh(session, settings.github_token, "POST", url,
              {"ref": settings.dev_ref, "inputs": {"target": target, "requested_by": who[:60]}})
    logger.info("dev load %s requested by %s", target, who)
    return {"ok": True, "target": target, "message": f"Loading {target} onto the dev server",
            "requested_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def loaded(settings) -> dict | None:
    """The status file, or None when the dev server has written none yet. Only the known fields are passed on."""
    try:
        with open(settings.dev_status_file, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        logger.warning("dev status file unreadable: %s", e)
        return None
    keep = ("label", "commit", "sha256", "loaded_at", "previous", "content", "dev_haks", "database", "health", "updated_at")
    return {k: data.get(k) for k in keep} if isinstance(data, dict) else None


async def status(session, settings) -> dict:
    try:
        run = await last_run(session, settings)
    except (DevError, aiohttp.ClientError, TimeoutError) as e:
        logger.warning("dev status: no workflow run: %s", e)
        run = None
    return {"loaded": loaded(settings) if settings.dev_status_file else None, "last_run": run}
