import json
from unittest.mock import AsyncMock

import pytest

from app.ssh import SshResult
from app.services import nwnx_version
from app.services.nwnx_version import (
    get_running, get_latest, parse_image, summary, update_available,
)

OLD = "4eaed6b10e3f48cc09eed62cd185d7c366107448"
NEW = "bbdec0ee4d1c2b3a4f5e6d7c8b9a0f1e2d3c4b5a"
LABELS = {
    "org.opencontainers.image.revision": NEW,
    "org.opencontainers.image.created": "2026-10-18T10:12:00Z",
    "com.nwn.version": "8193.37.17",
}


class FakeResponse:
    def __init__(self, body, status=200):
        self._body, self.status = body, status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    async def json(self):
        return self._body

    async def read(self):
        return json.dumps(self._body).encode()


class FakeSession:
    """Answers GETs by URL; records them so tests can count registry round trips."""
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, params=None, headers=None):
        self.calls.append(url)
        body = self.routes[url]
        return body if isinstance(body, FakeResponse) else FakeResponse(body)


REGISTRY = {
    "https://ghcr.io/token": {"token": "anon"},
    "https://ghcr.io/v2/nwnxee/unified/manifests/latest": {
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {"digest": "sha256:attest", "platform": {"os": "unknown", "architecture": "unknown"}},
            {"digest": "sha256:amd64", "platform": {"os": "linux", "architecture": "amd64"}},
        ],
    },
    "https://ghcr.io/v2/nwnxee/unified/manifests/sha256:amd64": {"config": {"digest": "sha256:cfg"}},
    "https://ghcr.io/v2/nwnxee/unified/blobs/sha256:cfg": {"config": {"Labels": LABELS}},
}


@pytest.fixture(autouse=True)
def clear_cache():
    nwnx_version._latest_cache.clear()
    nwnx_version._behind_cache.clear()


def test_parse_image():
    assert parse_image("ghcr.io/nwnxee/unified:latest") == ("ghcr.io", "nwnxee/unified", "latest")
    assert parse_image("ghcr.io/nwnxee/unified") == ("ghcr.io", "nwnxee/unified", "latest")
    assert parse_image("nwnxee/unified:ed79330") == ("docker.io", "nwnxee/unified", "ed79330")
    assert parse_image("debian") == ("docker.io", "library/debian", "latest")
    assert parse_image("localhost:5000/me/nwn:dev") == ("localhost:5000", "me/nwn", "dev")


@pytest.mark.asyncio
async def test_get_latest_from_docker_hub():
    hub = "https://registry-1.docker.io/v2/nwnxee/unified"
    session = FakeSession({
        "https://auth.docker.io/token": {"token": "anon"},
        f"{hub}/manifests/ed79330": {"config": {"digest": "sha256:cfg"}},
        f"{hub}/blobs/sha256:cfg": {"config": {"Labels": LABELS}},
    })
    assert (await get_latest(session, "nwnxee/unified:ed79330"))["revision"] == NEW


@pytest.mark.asyncio
async def test_get_running_reads_the_container_labels(mock_ssh):
    mock_ssh.run = AsyncMock(side_effect=[SshResult(0, "abc123\n", ""), SshResult(0, json.dumps(LABELS), "")])
    assert await get_running(mock_ssh) == {"revision": NEW, "created": "2026-10-18T10:12:00Z", "nwn": "8193.37.17"}


@pytest.mark.asyncio
async def test_get_running_reads_a_stopped_container_too(mock_ssh):
    # A full stop leaves the container exited; Deploy All must still see which build it ran.
    mock_ssh.run = AsyncMock(side_effect=[SshResult(0, "abc123\n", ""), SshResult(0, json.dumps(LABELS), "")])
    assert (await get_running(mock_ssh))["revision"] == NEW
    assert "docker ps -aq " in mock_ssh.run.call_args_list[0].args[0]


@pytest.mark.asyncio
async def test_get_running_without_a_container_or_on_errors(mock_ssh):
    mock_ssh.run = AsyncMock(return_value=SshResult(0, "", ""))
    assert (await get_running(mock_ssh))["revision"] is None
    mock_ssh.run = AsyncMock(side_effect=Exception("ssh down"))
    assert (await get_running(mock_ssh))["revision"] is None


@pytest.mark.asyncio
async def test_get_latest_walks_index_to_amd64_config_and_caches():
    session = FakeSession(REGISTRY)
    build = await get_latest(session, "ghcr.io/nwnxee/unified:latest")
    assert build == {"revision": NEW, "created": "2026-10-18T10:12:00Z", "nwn": "8193.37.17"}
    calls = len(session.calls)
    assert await get_latest(session, "ghcr.io/nwnxee/unified:latest") == build
    assert len(session.calls) == calls, "second call within 5 minutes is served from the cache"


@pytest.mark.asyncio
async def test_get_latest_registry_failure_is_unknown_not_an_exception():
    session = FakeSession({**REGISTRY, "https://ghcr.io/token": FakeResponse({}, status=503)})
    assert (await get_latest(session, "ghcr.io/nwnxee/unified:latest"))["revision"] is None


def test_update_available_only_when_both_known_and_different():
    known = lambda rev: {"revision": rev, "created": None, "nwn": None}
    assert update_available(known(OLD), known(NEW))
    assert not update_available(known(NEW), known(NEW))
    assert not update_available(known(None), known(NEW))
    assert not update_available(known(OLD), known(None))


@pytest.mark.asyncio
async def test_summary_links_and_how_far_behind(monkeypatch):
    monkeypatch.setattr(nwnx_version, "commits_behind", AsyncMock(return_value=15))
    running = {"revision": OLD, "created": "2026-04-19T16:55:44Z", "nwn": "8193.37.17"}
    latest = {"revision": NEW, "created": "2026-10-18T10:12:00Z", "nwn": "8193.37.17"}
    out = await summary(None, running, latest, "nwnxee/unified")
    assert out["running"]["short"] == "4eaed6b" and out["latest"]["short"] == "bbdec0e"
    assert out["running"]["url"] == f"https://github.com/nwnxee/unified/commit/{OLD}"
    assert out["update_available"] and out["behind"] == 15
    assert out["compare_url"] == "https://github.com/nwnxee/unified/compare/4eaed6b...bbdec0e"


@pytest.mark.asyncio
async def test_summary_up_to_date_has_no_compare(monkeypatch):
    behind = AsyncMock(return_value=0)
    monkeypatch.setattr(nwnx_version, "commits_behind", behind)
    build = {"revision": NEW, "created": None, "nwn": None}
    out = await summary(None, build, dict(build), "nwnxee/unified")
    assert not out["update_available"] and out["behind"] is None and out["compare_url"] is None
    behind.assert_not_called()


@pytest.mark.asyncio
async def test_commits_behind_is_asked_once_per_pair():
    url = f"https://api.github.com/repos/nwnxee/unified/compare/{OLD}...{NEW}"
    session = FakeSession({url: {"ahead_by": 22}})
    assert await nwnx_version.commits_behind(session, "nwnxee/unified", OLD, NEW) == 22
    assert await nwnx_version.commits_behind(session, "nwnxee/unified", OLD, NEW) == 22
    assert session.calls == [url]


def test_same_image_reads_docker_hub_names_alike():
    assert nwnx_version.same_image("nwnxee/unified:build8193.37.17", "docker.io/nwnxee/unified:build8193.37.17")
    assert not nwnx_version.same_image("nwnxee/unified:ed79330", "nwnxee/unified:build8193.37.17")
