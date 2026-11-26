"""What is going out, per repository in REPOS: the last release, what has been merged since, the latest CI runs, the run that stages what a
deploy would put live (the entry's #workflow, if given), and the open pull requests, with links."""

import asyncio
import logging
import time

import aiohttp

from app.config import Repo
from app.services.release import get_latest_tag

logger = logging.getLogger(__name__)

CACHE_SECONDS = 60
_cache: dict[str, tuple[float, dict]] = {}


async def _get(session: aiohttp.ClientSession, token: str, url: str):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
        resp.raise_for_status()
        return await resp.json()


def _commit(c: dict) -> dict:
    return {
        "sha": c["sha"][:7], "message": (c["commit"]["message"] or "").splitlines()[0],
        "author": (c.get("author") or {}).get("login") or c["commit"]["author"]["name"],
        "date": c["commit"]["author"]["date"], "url": c["html_url"],
    }


async def _one(session, token: str, repo: Repo, settings) -> dict:
    base = f"https://api.github.com/repos/{repo.full_name}"
    _, _, tag = await get_latest_tag(session, repo.full_name, token, settings.release_tag_regex)

    async def since():
        if not tag:
            return None
        data = await _get(session, token, f"{base}/compare/{tag}...{repo.branch}")
        commits = data["commits"]
        if settings.release_bump_message:
            # A release workflow that tags the merge and then commits a version bump on top: that commit is part of the release.
            bump = settings.release_bump_message.format(version=tag.removeprefix("v"))
            commits = [c for c in commits if (c["commit"]["message"] or "").splitlines()[:1] != [bump]]
        ahead = data["ahead_by"] - (len(data["commits"]) - len(commits))
        return {"ahead": ahead, "url": data["html_url"], "commits": [_commit(c) for c in commits[-15:]][::-1]}

    async def release():
        data = await _get(session, token, f"{base}/releases/latest")
        return {"tag": data["tag_name"], "url": data["html_url"], "published": data["published_at"]}

    async def runs():
        # Pushes only (manual runs also report the branch), and only each workflow's newest run:
        # an older run that failed or was cancelled has been superseded by the newer one.
        data = await _get(session, token, f"{base}/actions/runs?branch={repo.branch}&event=push&per_page=30")
        latest = {}
        for r in data["workflow_runs"]:  # newest first
            latest.setdefault(r["name"], r)
        return [{"name": r["name"], "status": r["status"], "conclusion": r["conclusion"], "url": r["html_url"], "at": r["created_at"],
                 "sha": r["head_sha"][:7]} for r in latest.values()]

    async def staging():
        if not repo.staging_workflow:
            return None
        data = await _get(session, token, f"{base}/actions/workflows/{repo.staging_workflow}/runs?per_page=1")
        if not data["workflow_runs"]:
            return None
        r = data["workflow_runs"][0]
        out = {"name": r["name"], "ref": r["head_branch"], "status": r["status"], "conclusion": r["conclusion"], "url": r["html_url"],
               "at": r["created_at"], "job": None, "step": None}
        if r["status"] == "completed" and r["conclusion"] == "success":
            return out
        # Still running or failed: which job and step it is at (or stopped in), so a page can say what it waits for.
        jobs = (await _get(session, token, f"{base}/actions/runs/{r['id']}/jobs?per_page=30"))["jobs"]
        done = r["status"] == "completed"
        job = next((j for j in jobs if (j["conclusion"] not in ("success", "skipped") if done else j["status"] != "completed")), None)
        if job:
            steps = job.get("steps") or []
            step = next((x for x in steps if (x["conclusion"] not in ("success", "skipped", None) if done else x["status"] != "completed")), None)
            out.update(job=job["name"], step=step["name"] if step else None, url=job["html_url"] or out["url"])
        return out

    async def pulls():
        data = await _get(session, token, f"{base}/pulls?state=open&per_page=15")
        return [{"number": p["number"], "title": p["title"], "url": p["html_url"], "draft": p["draft"], "author": p["user"]["login"],
                 "labels": [label["name"] for label in p["labels"]]} for p in data]

    results = await asyncio.gather(since(), release(), runs(), staging(), pulls(), return_exceptions=True)
    out = {"repo": repo.key, "full_name": repo.full_name, "branch": repo.branch, "url": f"https://github.com/{repo.full_name}", "tag": tag or None}
    for key, res in zip(("since", "release", "runs", "staging", "pulls"), results):
        if isinstance(res, BaseException):
            logger.warning("repo overview %s/%s: %s", repo.key, key, res)
            out[key] = None
        else:
            out[key] = res
    return out


async def overview(session, settings) -> dict:
    cached = _cache.get("all")
    if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1]
    repos = await asyncio.gather(*[_one(session, settings.github_token, r, settings) for r in settings.repo_map.values()])
    data = {"repos": list(repos)}
    _cache["all"] = (time.monotonic(), data)
    return data


def forget() -> None:
    """Drop the cached overview (a release was just made)."""
    _cache.clear()
