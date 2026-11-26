import json
import shlex
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.deploy import deploy_module, full_deploy, promote_haks_command
from app.ssh import SshResult
from tests.conftest import make_settings


def _settings(**kw):
    base = dict(game_dir="", mod_stage="/stage/mod", mod_live="/live/mod", hak_stage="/stage/hak", hak_live="/live/hak",
                tlk_live="/live/tlk", nwn_env_file="/env1", coolify_env_file="/env2", development_dir="/dev",
                coolify_url="https://coolify.test", coolify_token="t", coolify_service_uuid="test-uuid")
    base.update(kw)
    return make_settings(**base)


def _runs(fail=None):
    calls = []

    async def run(cmd, timeout=None):
        calls.append(cmd)
        if fail and cmd.startswith(fail):
            return SshResult(1, "", "disk full")
        return SshResult(0, "", "")
    return calls, run


async def _deploy(mock_ssh, mock_redis, mock_coolify, staged=True, settings=None):
    return await deploy_module(
        ssh=mock_ssh, redis_client=mock_redis, coolify=mock_coolify, http_session=AsyncMock(),
        module={"name": "MyWorld_6-23", "file": "MyWorld_6-23.mod", "staged": staged},
        wait_minutes=0, requester="test", settings=settings or _settings(),
    )


@pytest.mark.asyncio
async def test_deploy_promotes_staged_module_and_sets_nwn_module(mock_ssh, mock_redis, mock_coolify):
    calls, mock_ssh.run = _runs()
    errors = await _deploy(mock_ssh, mock_redis, mock_coolify)

    assert errors == []
    assert any(c.startswith("sh -c 'mv /stage/mod/*.mod /live/mod/'") for c in calls)
    sed = next(c for c in calls if c.startswith("sed"))
    assert "NWN_MODULE=MyWorld_6-23" in sed and "/env1" in sed and "/env2" in sed
    mock_coolify.patch_env.assert_called_once_with("test-uuid", "NWN_MODULE", "MyWorld_6-23")
    assert any(c.startswith("sh -c 'rm -f /dev/*") for c in calls), "hot-loaded overrides dropped after a good deploy"


@pytest.mark.asyncio
async def test_deploy_recreates_container_instead_of_in_game_restart(mock_ssh, mock_redis, mock_coolify):
    # An in-game restart keeps the container's old NWN_MODULE.
    calls, mock_ssh.run = _runs()
    with patch("app.services.deploy.redis_command", new=AsyncMock(return_value=(True, ""))) as redis_cmd:
        errors = await _deploy(mock_ssh, mock_redis, mock_coolify)

    assert errors == []
    stop = next(i for i, c in enumerate(calls) if "docker stop" in c)
    move = next(i for i, c in enumerate(calls) if c.startswith("sh -c 'mv /stage/mod"))
    assert stop < move, "container stops before files are swapped"
    mock_coolify.post.assert_called_once_with("/services/test-uuid/restart")
    redis_cmd.assert_not_called()


@pytest.mark.asyncio
async def test_deploy_failed_promote_keeps_old_module_and_haks_but_restarts(mock_ssh, mock_redis, mock_coolify):
    calls, mock_ssh.run = _runs(fail="sh -c 'mv /stage/mod")
    with patch("app.services.deploy.get_staged_files", new=AsyncMock(return_value=([], ["x.hak"]))):
        errors = await _deploy(mock_ssh, mock_redis, mock_coolify)

    assert any("Stage promote" in e for e in errors)
    assert not any(c.startswith("sed") for c in calls), "NWN_MODULE untouched"
    mock_coolify.patch_env.assert_not_called()
    assert not any("cd /stage/hak" in c for c in calls), "haks stay staged"
    assert not any("rm -f" in c for c in calls)
    mock_coolify.post.assert_called_once_with("/services/test-uuid/restart")


@pytest.mark.asyncio
async def test_deploy_moves_staged_haks_with_the_module(mock_ssh, mock_redis, mock_coolify):
    calls, mock_ssh.run = _runs()
    with patch("app.services.deploy.get_staged_files", new=AsyncMock(return_value=([], ["x.hak"]))):
        assert await _deploy(mock_ssh, mock_redis, mock_coolify) == []
    assert any(c.startswith("cd /stage/hak") for c in calls)


@pytest.mark.asyncio
async def test_failed_hak_move_is_reported(mock_ssh, mock_redis, mock_coolify):
    calls, mock_ssh.run = _runs(fail="cd /stage/hak")
    with patch("app.services.deploy.get_staged_files", new=AsyncMock(return_value=([], ["x.hak"]))):
        errors = await _deploy(mock_ssh, mock_redis, mock_coolify)
    assert any("Hak move" in e for e in errors)
    mock_coolify.post.assert_called_once()


@pytest.mark.asyncio
async def test_the_deploy_hook_runs_before_the_start_with_the_module_name(mock_ssh, mock_redis, mock_coolify):
    calls, mock_ssh.run = _runs()
    order = []
    mock_coolify.post = AsyncMock(side_effect=lambda *a: order.append("start"))
    orig = mock_ssh.run

    async def run(cmd, timeout=None):
        if "rebuild-nwsync" in cmd:
            order.append("hook")
        return await orig(cmd, timeout)
    mock_ssh.run = run
    assert await _deploy(mock_ssh, mock_redis, mock_coolify, settings=_settings(deploy_hook="~/bin/rebuild-nwsync")) == []
    assert any(c == "env MODULE=MyWorld_6-23 ~/bin/rebuild-nwsync 2>&1" for c in calls)
    assert order == ["hook", "start"]


@pytest.mark.asyncio
async def test_a_failed_hook_is_an_error_and_the_server_still_starts(mock_ssh, mock_redis, mock_coolify):
    calls, mock_ssh.run = _runs(fail="env MODULE=")
    errors = await _deploy(mock_ssh, mock_redis, mock_coolify, settings=_settings(deploy_hook="false"))
    assert any("Deploy hook" in e for e in errors)
    mock_coolify.post.assert_called_once()


@pytest.mark.asyncio
async def test_deploy_without_coolify_starts_with_the_start_command(mock_ssh, mock_redis):
    calls, mock_ssh.run = _runs()
    s = _settings(coolify_url="", coolify_env_file="", game_start_command="cd ~/game && docker compose up -d")
    assert await _deploy(mock_ssh, mock_redis, None, settings=s) == []
    assert calls[-1].startswith("cd ~/game && docker compose up -d")


@pytest.mark.asyncio
async def test_deploy_with_no_way_to_start_says_so(mock_ssh, mock_redis):
    calls, mock_ssh.run = _runs()
    errors = await _deploy(mock_ssh, mock_redis, None, settings=_settings(coolify_url=""))
    assert errors and "No way to start" in errors[-1]


@pytest.mark.asyncio
async def test_full_deploy_with_nothing_different_does_nothing(mock_ssh, mock_redis, mock_coolify):
    with patch("app.services.deploy.check_module_staged_differs", new=AsyncMock(return_value=False)), \
         patch("app.services.deploy.check_haks_staged_differs", new=AsyncMock(return_value=False)), \
         patch("app.services.deploy.nwnx_version.get_running", new=AsyncMock(return_value={"revision": "a"})), \
         patch("app.services.deploy.nwnx_version.get_latest", new=AsyncMock(return_value={"revision": "a"})), \
         patch("app.services.deploy.stop_server", new=AsyncMock()) as stop:
        result = await full_deploy(ssh=mock_ssh, redis_client=mock_redis, coolify=mock_coolify, http_session=AsyncMock(),
                                   wait_minutes=0, settings=_settings(), db_pool=None)
    assert result == {"ok": True, "applied": [], "message": "Nothing to deploy"}
    stop.assert_not_called()


@pytest.mark.asyncio
async def test_full_deploy_applies_module_and_haks(mock_ssh, mock_redis, mock_coolify):
    calls, run = _runs()

    async def ssh_run(cmd, timeout=None):
        if cmd.startswith("ls -t /stage/mod"):
            calls.append(cmd)
            return SshResult(0, "/stage/mod/MyWorld_7-0.mod", "")
        return await run(cmd, timeout)
    mock_ssh.run = ssh_run
    with patch("app.services.deploy.check_module_staged_differs", new=AsyncMock(return_value=True)), \
         patch("app.services.deploy.check_haks_staged_differs", new=AsyncMock(return_value=True)), \
         patch("app.services.deploy.nwnx_version.get_running", new=AsyncMock(return_value={"revision": None})), \
         patch("app.services.deploy.nwnx_version.get_latest", new=AsyncMock(return_value={"revision": None})), \
         patch("app.services.deploy.get_player_count", new=AsyncMock(return_value={"players": 0, "dms": 0})):
        result = await full_deploy(ssh=mock_ssh, redis_client=mock_redis, coolify=mock_coolify, http_session=AsyncMock(),
                                   wait_minutes=5, settings=_settings(), db_pool=None)
    assert result["ok"] and result["applied"] == ["module", "haks"]
    mock_coolify.patch_env.assert_called_once_with("test-uuid", "NWN_MODULE", "MyWorld_7-0")
    mock_coolify.post.assert_called_once_with("/services/test-uuid/restart")
    mock_redis.publish.assert_not_called()   # nobody on: no countdown


def test_promote_moves_the_talk_table_to_the_tlk_folder(tmp_path):
    stage, hak, tlk = tmp_path / "stage", tmp_path / "hak", tmp_path / "tlk"
    for d in (stage, hak, tlk):
        d.mkdir()
    (stage / "myhak.hak").write_text("new hak")
    (stage / "custom.tlk").write_text("new tlk")
    (tlk / "custom.tlk").write_text("old tlk")
    settings = MagicMock(hak_stage=str(stage), hak_live=str(hak), tlk_live=str(tlk), sudo="")
    result = subprocess.run(["bash", "-c", promote_haks_command(settings)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (hak / "myhak.hak").read_text() == "new hak"
    assert (tlk / "custom.tlk").read_text() == "new tlk"
    assert not (hak / "custom.tlk").exists()
    assert list(stage.iterdir()) == []


def test_promote_with_nothing_staged_is_fine(tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir()
    settings = MagicMock(hak_stage=str(stage), hak_live=str(tmp_path), tlk_live=str(tmp_path), sudo="")
    assert subprocess.run(["bash", "-c", promote_haks_command(settings)]).returncode == 0


@pytest.mark.asyncio
async def test_staged_talk_table_is_compared_with_the_tlk_folder(mock_ssh):
    from app.services.server import check_haks_staged_differs
    listings = {
        "ls -l /stage/hak/": "total 1\n-rw 1 a a 100 Nov 5 10:00 custom.tlk\n",
        "ls -l /live/hak/": "total 1\n-rw 1 a a 999 Nov 5 10:00 custom.tlk\n",   # an old copy nobody reads
        "ls -l /live/tlk/": "total 1\n-rw 1 a a 100 Nov 5 10:00 custom.tlk\n",
    }

    async def run(cmd, timeout=None):
        for prefix, out in listings.items():
            if cmd.startswith(prefix):
                return SshResult(0, "\n".join(f"{line.split()[4]} {line.split()[-1]}" for line in out.splitlines()[1:]), "")
        if cmd.startswith("md5sum"):     # every file named has the same digest, printed as md5sum does: "<digest>  <path>" per file
            paths = [w for w in shlex.split(cmd)[1:] if not w.startswith("2>")]
            return SshResult(0, "\n".join(f"abc  {p}" for p in paths), "")
        return SshResult(1, "", "")

    mock_ssh.run = run
    assert await check_haks_staged_differs(mock_ssh, "/stage/hak", "/live/hak", "/live/tlk") is False
    assert await check_haks_staged_differs(mock_ssh, "/stage/hak", "/live/hak") is True


@pytest.mark.asyncio
async def test_countdown_when_someone_is_on(mock_ssh, mock_redis, mock_coolify):
    mock_redis.get = AsyncMock(return_value=json.dumps({"status": "ok", "message": "ok"}))
    calls, mock_ssh.run = _runs()
    with patch("app.services.deploy.asyncio.sleep", new=AsyncMock()) as sleep:
        await deploy_module(ssh=mock_ssh, redis_client=mock_redis, coolify=mock_coolify, http_session=AsyncMock(),
                            module={"name": "MyWorld_6-23", "staged": True}, wait_minutes=2, requester="ci", settings=_settings())
    payload = json.loads(mock_redis.publish.call_args[0][1])
    assert payload["args"] == {"seconds": 120, "reason": "deploy v6.23 by ci"}
    sleep.assert_awaited_once_with(135)


@pytest.mark.asyncio
async def test_with_ssh_sudo_the_file_changes_run_under_sudo(mock_ssh, mock_redis, mock_coolify):
    calls, mock_ssh.run = _runs()
    s = _settings(ssh_sudo=True, deploy_hook="/usr/local/bin/hook")
    with patch("app.services.deploy.get_staged_files", new=AsyncMock(return_value=([], ["x.hak"]))):
        assert await _deploy(mock_ssh, mock_redis, mock_coolify, settings=s) == []
    changing = [c for c in calls if any(w in c for w in ("mv ", "sed ", "rm -f", "MODULE="))]
    assert changing and all("sudo -n " in c for c in changing)
    assert not any(c.startswith("sudo") for c in calls if c.startswith(("ls", "md5sum", "docker")))


@pytest.mark.asyncio
async def test_full_deploy_leaves_nwnx_out_for_a_game_on_another_tag(mock_ssh, mock_redis, mock_coolify):
    s = make_settings(coolify_url="https://c.test", coolify_token="t", coolify_service_uuid="u", nwnx_image="nwnxee/unified:build1")
    with patch("app.services.deploy.check_module_staged_differs", new=AsyncMock(return_value=False)), \
         patch("app.services.deploy.check_haks_staged_differs", new=AsyncMock(return_value=False)), \
         patch("app.services.deploy.nwnx_version.get_running", new=AsyncMock(return_value={"revision": "a"})), \
         patch("app.services.deploy.nwnx_version.get_latest", new=AsyncMock(return_value={"revision": "b"})), \
         patch("app.services.deploy.nwnx_version.get_configured_image", new=AsyncMock(return_value="nwnxee/unified:ed79330")):
        result = await full_deploy(ssh=mock_ssh, redis_client=mock_redis, coolify=mock_coolify, http_session=AsyncMock(),
                                   wait_minutes=0, settings=s, db_pool=None)
    assert result == {"ok": True, "applied": [], "message": "Nothing to deploy"}
    mock_coolify.post.assert_not_awaited()
