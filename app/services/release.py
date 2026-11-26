"""GitHub releases of the repositories in REPOS. The notes are GitHub's own (generated from the pull requests merged since the last release),
so no changelog table is needed."""

import logging
import re

import aiohttp

from app.config import Repo

logger = logging.getLogger(__name__)

API = "https://api.github.com"


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}


async def get_latest_tag(session: aiohttp.ClientSession, full_name: str, github_token: str,
                         tag_regex: str = r"^v(\d+)\.(\d+)$") -> tuple[int, int, str]:
    """(major, minor, tag) of the newest tag matching `tag_regex` (groups 1 and 2), or (0, 0, "")."""
    async with session.get(f"{API}/repos/{full_name}/tags?per_page=50", headers=_headers(github_token)) as resp:
        resp.raise_for_status()
        tags = await resp.json()
    pattern = re.compile(tag_regex)
    for t in tags:
        name = t.get("name", "")
        m = pattern.match(name)
        if m:
            return int(m.group(1)), int(m.group(2)), name
    return 0, 0, ""


def next_tags(major: int, minor: int, tag_format: str) -> dict:
    return {"next_minor": tag_format.format(major=major, minor=minor + 1),
            "next_major": tag_format.format(major=major + 1, minor=0)}


async def generate_notes(session: aiohttp.ClientSession, github_token: str, repo: Repo, tag: str, previous: str = "") -> str:
    """GitHub's release notes for `tag` on the repository's branch (what a release would say), without making anything."""
    payload = {"tag_name": tag, "target_commitish": repo.branch}
    if previous:
        payload["previous_tag_name"] = previous
    async with session.post(f"{API}/repos/{repo.full_name}/releases/generate-notes", headers=_headers(github_token), json=payload) as resp:
        data = await resp.json()
        if resp.status != 200:
            raise RuntimeError(data.get("message", f"HTTP {resp.status}"))
        return data.get("body", "")


async def create_release(session: aiohttp.ClientSession, github_token: str, repo: Repo, tag: str, name: str) -> dict:
    payload = {
        "tag_name": tag,
        "name": name,
        "draft": False,
        "prerelease": False,
        "target_commitish": repo.branch,
        "generate_release_notes": True,
    }
    async with session.post(f"{API}/repos/{repo.full_name}/releases", headers=_headers(github_token), json=payload) as resp:
        data = await resp.json()
        if resp.status == 201:
            return {"ok": True, "tag": tag, "url": data.get("html_url", "")}
        return {"ok": False, "message": data.get("message", f"HTTP {resp.status}")}
