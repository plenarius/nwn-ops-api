"""Which NWNX build is running and which one an NWNX update would pull.

Builds are identified by the commit they were built from (the image's
org.opencontainers.image.revision label). The NWN version label is the same for every
NWNX build of a game release, so it can't tell two NWNX builds apart.
"""
import json
import shlex
import logging
import time

import aiohttp

from app.ssh import SshClient

logger = logging.getLogger(__name__)

REVISION_LABEL = "org.opencontainers.image.revision"
CREATED_LABEL = "org.opencontainers.image.created"
NWN_LABEL = "com.nwn.version"

_MANIFEST_TYPES = ", ".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
))
_LATEST_TTL = 300.0
_latest_cache: dict[str, tuple[float, dict]] = {}
_behind_cache: dict[tuple[str, str, str], int] = {}   # the distance between two commits never changes


def _build(labels: dict | None) -> dict:
    labels = labels or {}
    return {
        "revision": labels.get(REVISION_LABEL) or None,
        "created": labels.get(CREATED_LABEL) or None,
        "nwn": labels.get(NWN_LABEL) or None,
    }


def short(revision: str | None) -> str | None:
    return revision[:7] if revision else None


async def get_running(ssh: SshClient, name_filter: str = "nwserver") -> dict:
    """Labels of the image the game container was created from, stopped or not (newest container first)."""
    try:
        cid = await ssh.run(f"docker ps -aq --filter name={shlex.quote(name_filter)}")
        if cid.returncode != 0 or not cid.stdout.strip():
            return _build(None)
        container = cid.stdout.strip().splitlines()[0]
        result = await ssh.run(f"docker inspect --format '{{{{json .Config.Labels}}}}' {container}")
        if result.returncode != 0:
            return _build(None)
        return _build(json.loads(result.stdout))
    except Exception:
        logger.exception("could not read the running NWNX image")
        return _build(None)


async def get_configured_image(ssh: SshClient, name_filter: str = "nwserver") -> str:
    """The image name the game container was made from (its compose `image:`), or "" when unknown."""
    try:
        r = await ssh.run(f"docker inspect --format '{{{{.Config.Image}}}}' "
                          f"$(docker ps -aq --filter name={shlex.quote(name_filter)} | head -1) 2>/dev/null")
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def same_image(a: str, b: str) -> bool:
    """nwnxee/unified:x and docker.io/nwnxee/unified:x are one image."""
    return parse_image(a) == parse_image(b)


async def get_local_image(ssh: SshClient, image: str) -> dict:
    """Labels of the image as pulled on the game server host (what a restart would run)."""
    try:
        result = await ssh.run(f"docker image inspect --format '{{{{json .Config.Labels}}}}' {image}")
        if result.returncode != 0:
            return _build(None)
        return _build(json.loads(result.stdout))
    except Exception:
        logger.exception("could not read the local %s image", image)
        return _build(None)


def parse_image(image: str) -> tuple[str, str, str]:
    """ghcr.io/nwnxee/unified:latest -> ("ghcr.io", "nwnxee/unified", "latest"); a Docker Hub name (nwnxee/unified:ed79330, debian) is on
    docker.io, with library/ for an official image."""
    name, tag = image, "latest"
    head, _, last = image.rpartition("/")
    if ":" in last:
        last, _, tag = last.partition(":")
        name = f"{head}/{last}" if head else last
    first, _, rest = name.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        return first, rest, tag
    return "docker.io", name if rest else f"library/{name}", tag


def _endpoints(registry: str, repo: str) -> tuple[str, str, dict]:
    """(the registry's API base, its token URL, the token query) for anonymous pulls."""
    if registry == "docker.io":
        return f"https://registry-1.docker.io/v2/{repo}", "https://auth.docker.io/token", {
            "service": "registry.docker.io", "scope": f"repository:{repo}:pull"}
    return f"https://{registry}/v2/{repo}", f"https://{registry}/token", {"scope": f"repository:{repo}:pull"}


async def _fetch_latest(session: aiohttp.ClientSession, image: str) -> dict:
    registry, repo, tag = parse_image(image)
    base, token_url, params = _endpoints(registry, repo)
    async with session.get(token_url, params=params) as resp:
        resp.raise_for_status()
        token = (await resp.json())["token"]
    headers = {"Authorization": f"Bearer {token}", "Accept": _MANIFEST_TYPES}

    async with session.get(f"{base}/manifests/{tag}", headers=headers) as resp:
        resp.raise_for_status()
        manifest = json.loads(await resp.read())
    if "manifests" in manifest:   # multi-platform index: take linux/amd64
        digest = next(m["digest"] for m in manifest["manifests"]
                      if m.get("platform", {}).get("os") == "linux"
                      and m.get("platform", {}).get("architecture") == "amd64")
        async with session.get(f"{base}/manifests/{digest}", headers=headers) as resp:
            resp.raise_for_status()
            manifest = json.loads(await resp.read())

    async with session.get(f"{base}/blobs/{manifest['config']['digest']}", headers=headers) as resp:
        resp.raise_for_status()
        config = json.loads(await resp.read())
    return _build((config.get("config") or {}).get("Labels"))


async def get_latest(session: aiohttp.ClientSession, image: str) -> dict:
    """Labels of the image an NWNX update would pull, read from the registry (cached 5 minutes)."""
    cached = _latest_cache.get(image)
    if cached and time.monotonic() - cached[0] < _LATEST_TTL:
        return cached[1]
    try:
        build = await _fetch_latest(session, image)
    except Exception:
        logger.exception("could not read %s from the registry", image)
        return cached[1] if cached else _build(None)
    _latest_cache[image] = (time.monotonic(), build)
    return build


async def commits_behind(session: aiohttp.ClientSession, repo: str, base: str, head: str,
                         github_token: str = "") -> int | None:
    if (repo, base, head) in _behind_cache:
        return _behind_cache[(repo, base, head)]
    headers = {"Accept": "application/vnd.github+json"}
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"
    try:
        async with session.get(f"https://api.github.com/repos/{repo}/compare/{base}...{head}",
                               headers=headers) as resp:
            if resp.status != 200:
                return None
            ahead = (await resp.json()).get("ahead_by")
    except Exception:
        return None
    if isinstance(ahead, int):
        _behind_cache[(repo, base, head)] = ahead
    return ahead


def update_available(running: dict, latest: dict) -> bool:
    """Only when both builds are known and differ: unknown is never a reason to restart."""
    return bool(running["revision"] and latest["revision"] and running["revision"] != latest["revision"])


async def summary(session: aiohttp.ClientSession, running: dict, latest: dict, repo: str,
                  github_token: str = "") -> dict:
    """What the dashboard shows: both builds, links, and how far behind the running one is."""
    def link(build):
        return f"https://github.com/{repo}/commit/{build['revision']}" if build["revision"] else None

    out = {
        "running": {**running, "short": short(running["revision"]), "url": link(running)},
        "latest": {**latest, "short": short(latest["revision"]), "url": link(latest)},
        "update_available": update_available(running, latest),
        "behind": None,
        "compare_url": None,
    }
    if out["update_available"]:
        out["behind"] = await commits_behind(session, repo, running["revision"], latest["revision"], github_token)
        out["compare_url"] = f"https://github.com/{repo}/compare/{short(running['revision'])}...{short(latest['revision'])}"
    return out
