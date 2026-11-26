import json
from unittest.mock import AsyncMock

import pytest

from app.redis_client import NO_REDIS
from app.services.restart import NO_START, cancel_restart, hard_restart, restart_server, start_game, stop_server
from app.ssh import SshResult
from tests.conftest import make_settings


@pytest.mark.asyncio
async def test_restart_server_sends_redis_command(mock_redis, mock_ssh, settings):
    mock_redis.get = AsyncMock(return_value=json.dumps({"status": "ok", "message": "restarting"}))

    ok, msg = await restart_server(mock_redis, mock_ssh, None, settings, seconds=300, reason="test")

    assert ok is True
    mock_redis.publish.assert_called_once()
    channel, raw = mock_redis.publish.call_args[0]
    assert channel == "nwn:cmd"
    payload = json.loads(raw)
    assert payload["cmd"] == "restart" and payload["args"] == {"seconds": 300, "reason": "test"}
    assert mock_redis.get.call_args[0][0] == f"nwn:response:{payload['request_id']}"
    assert mock_redis.set.call_args[0][0] == "nwn:restart_pending"


@pytest.mark.asyncio
async def test_the_prefix_is_a_setting(mock_redis, mock_ssh):
    mock_redis.get = AsyncMock(return_value=json.dumps({"status": "ok", "message": "restarting"}))
    await restart_server(mock_redis, mock_ssh, None, make_settings(redis_prefix="myworld:"), seconds=60, reason="x")
    assert mock_redis.publish.call_args[0][0] == "myworld:cmd"


@pytest.mark.asyncio
async def test_restart_fallback_to_coolify_when_server_stopped(mock_redis, mock_ssh, mock_coolify, settings):
    mock_redis.get = AsyncMock(return_value=None)
    mock_ssh.run = AsyncMock(return_value=SshResult(1, "", ""))

    ok, msg = await restart_server(mock_redis, mock_ssh, mock_coolify, settings, seconds=60, reason="test")

    assert ok is True and "Coolify" in msg
    mock_coolify.post.assert_called_once_with("/services/test-uuid/restart")


@pytest.mark.asyncio
async def test_without_redis_a_running_server_is_left_to_the_caller(mock_ssh, mock_coolify, settings):
    mock_ssh.run = AsyncMock(return_value=SshResult(0, "running|Up 2 hours (healthy)", ""))
    ok, msg = await restart_server(None, mock_ssh, mock_coolify, settings, seconds=60, reason="test")
    assert (ok, msg) == (False, NO_REDIS)
    mock_coolify.post.assert_not_called()


@pytest.mark.asyncio
async def test_hard_restart_stops_then_starts(mock_ssh, mock_coolify, settings):
    calls = []

    async def run(cmd, timeout=None):
        calls.append(cmd)
        return SshResult(0, "", "")
    mock_ssh.run = run
    ok, _ = await hard_restart(mock_ssh, mock_coolify, settings)
    assert ok and "docker stop" in calls[0]
    mock_coolify.post.assert_called_once_with("/services/test-uuid/restart")


@pytest.mark.asyncio
async def test_start_without_coolify_uses_the_start_command(mock_ssh):
    mock_ssh.run = AsyncMock(return_value=SshResult(0, "", ""))
    ok, msg = await start_game(mock_ssh, None, make_settings(game_start_command="cd ~/game && docker compose up -d"))
    assert ok
    assert mock_ssh.run.call_args[0][0].startswith("cd ~/game && docker compose up -d")


@pytest.mark.asyncio
async def test_start_with_nothing_configured_says_what_to_set(mock_ssh):
    assert await start_game(mock_ssh, None, make_settings()) == (False, NO_START)


@pytest.mark.asyncio
async def test_cancel_restart(mock_redis, settings):
    mock_redis.get = AsyncMock(return_value=json.dumps({"status": "ok", "message": "cancelled"}))

    ok, msg = await cancel_restart(mock_redis, settings)

    assert ok is True
    mock_redis.delete.assert_called_once_with("nwn:restart_pending")


@pytest.mark.asyncio
async def test_stop_server_docker_stop(mock_ssh):
    mock_ssh.run = AsyncMock(return_value=SshResult(0, "stopped", ""))

    ok, msg = await stop_server(mock_ssh, "nwserver")

    assert ok is True
    assert "--filter name=nwserver" in mock_ssh.run.call_args[0][0]
