from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.nwnx import pull_image, update_nwnx
from app.ssh import SshResult

IMAGE = "ghcr.io/nwnxee/unified:latest"


@pytest.mark.asyncio
async def test_pull_image_new_image(mock_ssh):
    mock_ssh.run = AsyncMock(return_value=SshResult(0, "Pulling from ghcr.io\nStatus: Downloaded newer image", ""))
    pulled, msg = await pull_image(mock_ssh, IMAGE)
    assert pulled is True


@pytest.mark.asyncio
async def test_pull_image_already_up_to_date(mock_ssh):
    mock_ssh.run = AsyncMock(return_value=SshResult(0, "Status: Image is up to date", ""))
    pulled, msg = await pull_image(mock_ssh, IMAGE)
    assert pulled is False


@pytest.mark.asyncio
async def test_a_failed_pull_is_not_a_new_image(mock_ssh):
    mock_ssh.run = AsyncMock(return_value=SshResult(1, "", "manifest unknown"))
    pulled, msg = await pull_image(mock_ssh, IMAGE)
    assert pulled is False and "manifest unknown" in msg


@pytest.mark.asyncio
async def test_update_nwnx_skips_restart_when_stopped(mock_ssh, mock_redis, settings):
    mock_ssh.run = AsyncMock(side_effect=[
        SshResult(0, "Status: Downloaded newer image", ""),  # pull
        SshResult(1, "", ""),  # docker ps (server not running)
    ])

    result = await update_nwnx(ssh=mock_ssh, redis_client=mock_redis, coolify=AsyncMock(), http_session=AsyncMock(), wait_minutes=5,
                               db_pool=MagicMock(), settings=settings)

    assert result["pulled"] is True
    assert "stopped" in result["message"].lower()


OLD_LABELS = '{"org.opencontainers.image.revision": "4eaed6b10e3f48cc09eed62cd185d7c366107448"}'
NEW_LABELS = '{"org.opencontainers.image.revision": "bbdec0ee4054e661a10f15b007880a3084fdf337"}'


def _update_kwargs(mock_ssh, mock_redis, coolify, settings):
    return dict(ssh=mock_ssh, redis_client=mock_redis, coolify=coolify, http_session=AsyncMock(), wait_minutes=0, db_pool=MagicMock(),
                settings=settings)


@pytest.mark.asyncio
async def test_update_restarts_when_the_image_was_already_pulled_but_the_server_is_older(mock_ssh, mock_redis, monkeypatch, settings):
    # What a dashboard does: /nwnx/pull first, so this pull finds nothing new. It must still restart.
    from app.services import nwnx
    monkeypatch.setattr(nwnx, "get_player_count", AsyncMock(return_value={"players": 0, "dms": 0}))
    monkeypatch.setattr(nwnx, "stop_server", AsyncMock())
    monkeypatch.setattr(nwnx.notify, "send", AsyncMock())
    mock_ssh.run = AsyncMock(side_effect=[
        SshResult(0, "Status: Image is up to date", ""),        # pull
        SshResult(0, "running|Up 2 hours (healthy)", ""),       # server status
        SshResult(0, "abc123\n", ""), SshResult(0, OLD_LABELS, ""),   # running container
        SshResult(0, NEW_LABELS, ""),                           # local image
    ])
    coolify = AsyncMock()
    result = await update_nwnx(**_update_kwargs(mock_ssh, mock_redis, coolify, settings))
    assert result["ok"] and result["message"] == "NWNX updated, server restarting"
    coolify.post.assert_awaited_once_with("/services/test-uuid/restart")
    nwnx.stop_server.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_does_nothing_when_already_on_the_pulled_build(mock_ssh, mock_redis, settings):
    mock_ssh.run = AsyncMock(side_effect=[
        SshResult(0, "Status: Image is up to date", ""),
        SshResult(0, "running|Up 2 hours (healthy)", ""),
        SshResult(0, "abc123\n", ""), SshResult(0, NEW_LABELS, ""),
        SshResult(0, NEW_LABELS, ""),
    ])
    coolify = AsyncMock()
    result = await update_nwnx(**_update_kwargs(mock_ssh, mock_redis, coolify, settings))
    assert result["message"] == "Already running the latest NWNX build (bbdec0e)."
    coolify.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_waits_with_a_countdown_when_players_are_online(mock_ssh, mock_redis, monkeypatch, settings):
    from app.services import nwnx
    monkeypatch.setattr(nwnx, "get_player_count", AsyncMock(return_value={"players": 3, "dms": 0}))
    monkeypatch.setattr(nwnx, "stop_server", AsyncMock())
    monkeypatch.setattr(nwnx.notify, "send", AsyncMock())
    monkeypatch.setattr(nwnx, "redis_command", AsyncMock())
    monkeypatch.setattr(nwnx, "set_restart_pending", AsyncMock())
    monkeypatch.setattr(nwnx.asyncio, "sleep", AsyncMock())
    mock_ssh.run = AsyncMock(side_effect=[
        SshResult(0, "Status: Downloaded newer image", ""),
        SshResult(0, "running|Up 2 hours (healthy)", ""),
        SshResult(0, "abc123\n", ""), SshResult(0, OLD_LABELS, ""),
        SshResult(0, NEW_LABELS, ""),
    ])
    kwargs = _update_kwargs(mock_ssh, mock_redis, AsyncMock(), settings)
    kwargs["wait_minutes"] = 15
    await update_nwnx(**kwargs)
    nwnx.redis_command.assert_awaited_once_with(mock_redis, "restart", {"seconds": 900, "reason": "nwnx-update"}, "nwn:", 5.0)
    nwnx.asyncio.sleep.assert_awaited_once_with(915)
