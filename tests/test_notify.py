import logging
from datetime import datetime, timezone

import pytest

from app import notify
from tests.conftest import make_settings

HOOK = "https://discord.com/api/webhooks/1/hook-token-secret"


class _Resp:
    def __init__(self, status):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    closed = False

    def __init__(self, status=204, boom=None):
        self.status, self.boom, self.posts = status, boom, []

    def post(self, url, json=None, timeout=None):
        if self.boom:
            raise self.boom
        self.posts.append((url, json))
        return _Resp(self.status)


def test_payload():
    s = make_settings(discord_username="Gatekeeper", discord_avatar_url="https://example.org/a.png")
    p = notify.payload(s, "server.restart_scheduled", "In **5 minutes**.", version="1.2", now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert p["username"] == "Gatekeeper" and p["avatar_url"] == "https://example.org/a.png"
    e = p["embeds"][0]
    assert e["title"] == "Server restart scheduled" and e["description"] == "In **5 minutes**." and e["footer"] == {"text": "Version 1.2"}


@pytest.mark.asyncio
async def test_send_posts_to_the_webhook():
    session = _Session()
    assert await notify.send(make_settings(discord_webhook_url=HOOK), session, "server.restarting", "Now.") is True
    assert session.posts[0][0] == HOOK


@pytest.mark.asyncio
async def test_unset_webhook_only_logs(caplog):
    caplog.set_level(logging.INFO, logger="app.notify")
    assert await notify.send(make_settings(), _Session(), "server.restarting", "Now.") is False
    assert "not sent" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("session", [_Session(status=404), _Session(boom=RuntimeError(f"failed to reach {HOOK}"))])
async def test_failures_never_raise_or_log_the_webhook(session, caplog):
    caplog.set_level(logging.INFO)
    assert await notify.send(make_settings(discord_webhook_url=HOOK), session, "server.restarting", "Now.") is False
    assert "hook-token-secret" not in caplog.text


def test_minutes():
    assert notify.minutes(1) == "1 minute" and notify.minutes(5) == "5 minutes"
