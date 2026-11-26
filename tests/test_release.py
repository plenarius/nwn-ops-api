import pytest

from app.config import Repo
from app.services import release


class _Resp:
    def __init__(self, status, data):
        self.status, self._data = status, data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    async def json(self):
        return self._data


class _Session:
    def __init__(self, tags=None, post_status=201):
        self.tags, self.post_status, self.posted = tags or [], post_status, []

    def get(self, url, headers=None):
        return _Resp(200, self.tags)

    def post(self, url, headers=None, json=None):
        self.posted.append((url, json))
        return _Resp(self.post_status, {"html_url": "https://github.com/me/mod/releases/tag/v1.3", "body": "notes", "message": "nope"})


@pytest.mark.asyncio
async def test_latest_tag_follows_the_pattern():
    s = _Session(tags=[{"name": "nightly"}, {"name": "v1.2"}, {"name": "v1.1"}])
    assert await release.get_latest_tag(s, "me/mod", "t") == (1, 2, "v1.2")
    s = _Session(tags=[{"name": "release-3-4"}])
    assert await release.get_latest_tag(s, "me/mod", "t", r"^release-(\d+)-(\d+)$") == (3, 4, "release-3-4")
    assert await release.get_latest_tag(_Session(), "me/mod", "t") == (0, 0, "")


def test_next_tags():
    assert release.next_tags(1, 2, "v{major}.{minor}") == {"next_minor": "v1.3", "next_major": "v2.0"}
    assert release.next_tags(1, 2, "release-{major}-{minor:02d}") == {"next_minor": "release-1-03", "next_major": "release-2-00"}


@pytest.mark.asyncio
async def test_create_release_on_the_repos_branch_with_generated_notes():
    s = _Session()
    out = await release.create_release(s, "t", Repo("module", "me/mod", "develop"), "v1.3", "My world v1.3")
    assert out == {"ok": True, "tag": "v1.3", "url": "https://github.com/me/mod/releases/tag/v1.3"}
    url, body = s.posted[0]
    assert url == "https://api.github.com/repos/me/mod/releases"
    assert body["target_commitish"] == "develop" and body["name"] == "My world v1.3" and body["generate_release_notes"] is True


@pytest.mark.asyncio
async def test_create_release_failure():
    out = await release.create_release(_Session(post_status=422), "t", Repo("m", "me/mod", "main"), "v1.3", "v1.3")
    assert out == {"ok": False, "message": "nope"}
