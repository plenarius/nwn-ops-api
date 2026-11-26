import pytest

from app.services import repo_overview as ro
from tests.conftest import make_settings


def _s(**kw):
    return make_settings(github_token="t", repos="module=me/my-module@main#release.yml,haks=me/my-haks@main", **kw)


class FakeSession:
    def __init__(self, fail=(), over=None, seen=None):
        self.fail = fail
        self.over = over or {}  # url fragment -> response, instead of ro_data's
        self.seen = seen if seen is not None else []

    def get(self, url, **kw):
        outer = self
        self.seen.append(url)

        class Ctx:
            async def __aenter__(self_):
                class R:
                    def raise_for_status(self__):
                        if any(f in url for f in outer.fail):
                            raise RuntimeError("boom")

                    async def json(self__):
                        return next((v for k, v in outer.over.items() if k in url), None) or ro_data(url)
                return R()

            async def __aexit__(self_, *a):
                return False
        return Ctx()


def ro_data(url):
    if "/tags" in url:
        return [{"name": "v6.30"}]
    if "/compare/" in url:
        c = {"sha": "abcdef123", "commit": {"message": "fix a\n\nbody", "author": {"name": "N", "date": "2026-10-31T00:00:00Z"}},
             "author": {"login": "someone"}, "html_url": "https://x/c"}
        return {"ahead_by": 2, "html_url": "https://x/compare", "commits": [c, c]}
    if "/releases/latest" in url:
        return {"tag_name": "v6.30", "html_url": "https://x/r", "published_at": "2026-10-24T00:00:00Z"}
    if "/actions/workflows/" in url:
        return {"workflow_runs": [{"id": 1, "name": "Release", "head_branch": "v6.30", "status": "completed", "conclusion": "success",
                                   "html_url": "https://x/rel", "created_at": "2026-10-24T00:00:00Z"}]}
    if "/actions/runs" in url:
        return {"workflow_runs": [{"name": "Build", "status": "completed", "conclusion": "success", "html_url": "https://x/run",
                                   "created_at": "2026-10-31T00:00:00Z", "head_sha": "abcdef123"}]}
    return [{"number": 5, "title": "T", "html_url": "https://x/p", "draft": False, "user": {"login": "a"}, "labels": [{"name": "ready"}]}]


@pytest.fixture(autouse=True)
def _fresh():
    ro._cache.clear()


@pytest.mark.asyncio
async def test_overview_collects_release_commits_runs_and_pulls():
    seen = []
    data = await ro.overview(FakeSession(seen=seen), _s())
    mod, haks = data["repos"]
    assert mod["repo"] == "module" and mod["full_name"] == "me/my-module" and mod["tag"] == "v6.30" and mod["release"]["url"] == "https://x/r"
    assert mod["since"]["ahead"] == 2 and mod["since"]["commits"][0]["message"] == "fix a" and mod["since"]["commits"][0]["sha"] == "abcdef1"
    assert mod["runs"][0]["conclusion"] == "success" and mod["pulls"][0]["labels"] == ["ready"]
    assert mod["staging"]["conclusion"] == "success" and haks["staging"] is None
    assert any("/repos/me/my-module/compare/v6.30...main" in u for u in seen)
    assert not any("/actions/workflows/" in u and "my-haks" in u for u in seen), "no workflow given, none asked for"


def _c(sha, msg):
    return {"sha": sha, "commit": {"message": msg, "author": {"name": "CI", "date": "2026-11-08T00:45:00Z"}}, "author": None,
            "html_url": f"https://x/{sha}"}


def _run(name, conclusion, sha, at):
    return {"name": name, "status": "completed", "conclusion": conclusion, "html_url": f"https://x/{name}/{sha}", "created_at": at, "head_sha": sha}


@pytest.mark.asyncio
async def test_the_releases_own_version_bump_is_not_waiting_to_go_out():
    compare = {"ahead_by": 3, "html_url": "https://x/compare", "commits": [
        _c("aaaaaaa1", "chore: bump version to 6.30 [skip ci]"),
        _c("bbbbbbb2", "fix b"),
        _c("ccccccc3", "chore: bump version to 6.31 [skip ci]"),
    ]}
    data = await ro.overview(FakeSession(over={"/compare/": compare}), _s(release_bump_message="chore: bump version to {version} [skip ci]"))
    since = data["repos"][0]["since"]
    # Only the bump for the tagged version (6.30) belongs to the release; anything else is still listed.
    assert since["ahead"] == 2 and [c["message"] for c in since["commits"]] == ["chore: bump version to 6.31 [skip ci]", "fix b"]


@pytest.mark.asyncio
async def test_without_a_bump_message_every_commit_is_listed():
    compare = {"ahead_by": 1, "html_url": "https://x/compare", "commits": [_c("aaaaaaa1", "chore: bump version to 6.30 [skip ci]")]}
    data = await ro.overview(FakeSession(over={"/compare/": compare}), _s())
    assert data["repos"][0]["since"]["ahead"] == 1


@pytest.mark.asyncio
async def test_runs_keep_only_each_workflows_newest_push_run():
    runs = {"workflow_runs": [
        _run("Tests", "success", "new0000", "2026-11-08T01:00:00Z"),
        _run("Build", "success", "new0000", "2026-11-08T01:00:00Z"),
        _run("Tests", "failure", "old0000", "2026-11-08T00:40:00Z"),
        _run("Build", "cancelled", "old0000", "2026-11-08T00:40:00Z"),
    ]}
    seen = []
    data = await ro.overview(FakeSession(over={"/actions/runs": runs}, seen=seen), _s())
    got = [(r["name"], r["conclusion"], r["sha"]) for r in data["repos"][0]["runs"]]
    assert got == [("Tests", "success", "new0000"), ("Build", "success", "new0000")]
    assert any("/actions/runs" in u and "event=push" in u for u in seen)


@pytest.mark.asyncio
async def test_one_failing_call_leaves_the_rest():
    data = await ro.overview(FakeSession(fail=("/actions/runs",)), _s())
    assert data["repos"][0]["runs"] is None and data["repos"][0]["since"] is not None


def _job(name, status, conclusion, steps):
    return {"name": name, "status": status, "conclusion": conclusion, "html_url": f"https://x/job/{name}",
            "steps": [{"name": n, "status": st, "conclusion": c} for n, st, c in steps]}


def _release_run(status, conclusion):
    return {"workflow_runs": [{"id": 77, "name": "Release", "head_branch": "v8.0", "status": status, "conclusion": conclusion,
                               "html_url": "https://x/run/77", "created_at": "2026-11-14T00:00:00Z"}]}


@pytest.mark.asyncio
async def test_a_release_run_still_going_says_which_job_and_step():
    jobs = {"jobs": [
        _job("build", "completed", "success", [("Build the module", "completed", "success")]),
        _job("stage", "in_progress", None, [("Set up job", "completed", "success"), ("Copy to the game host", "in_progress", None),
                                            ("Complete job", "queued", None)]),
    ]}
    data = await ro.overview(FakeSession(over={"/runs/77/jobs": jobs, "/actions/workflows/": _release_run("in_progress", None)}), _s())
    st = data["repos"][0]["staging"]
    assert st["status"] == "in_progress" and st["conclusion"] is None and st["ref"] == "v8.0"
    assert st["job"] == "stage" and st["step"] == "Copy to the game host" and st["url"] == "https://x/job/stage"


@pytest.mark.asyncio
async def test_a_failed_release_run_names_the_failed_step():
    jobs = {"jobs": [
        _job("build", "completed", "success", [("Build the module", "completed", "success")]),
        _job("stage", "completed", "failure", [("Set up job", "completed", "success"), ("Copy to the game host", "completed", "failure"),
                                               ("Complete job", "completed", "success")]),
    ]}
    data = await ro.overview(FakeSession(over={"/runs/77/jobs": jobs, "/actions/workflows/": _release_run("completed", "failure")}), _s())
    st = data["repos"][0]["staging"]
    assert st["conclusion"] == "failure" and st["job"] == "stage" and st["step"] == "Copy to the game host"


@pytest.mark.asyncio
async def test_a_refused_token_is_each_repositorys_error_not_an_exception():
    data = await ro.overview(FakeSession(fail=("/tags",)), _s())
    assert [r["repo"] for r in data["repos"]] == ["module", "haks"]
    assert all(r["error"] == "boom" and r["tag"] is None for r in data["repos"])
