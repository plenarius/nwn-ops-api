import logging
from unittest.mock import MagicMock

import pytest

from app.coolify import CoolifyClient

SECRET = "s3cret-value"


class _Resp:
    def __init__(self, status: int, body: str = "", data=None):
        self.status = status
        self._body = body
        self._data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    async def text(self):
        return self._body

    async def json(self):
        return self._data


def _client(patch_status: int) -> CoolifyClient:
    session = MagicMock()
    session.get.return_value = _Resp(200, data=[{"uuid": "env-1", "key": "NWN_MODULE"}])
    # Coolify's answer echoes the variable, value included.
    session.patch.return_value = _Resp(patch_status, body=f'{{"key":"NWN_MODULE","value":"{SECRET}"}}')
    return CoolifyClient("https://coolify.test", "token", session)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 201])
async def test_patch_env_success_is_not_an_error(status, caplog):
    caplog.set_level(logging.INFO, logger="app.coolify")
    await _client(status).patch_env("svc", "NWN_MODULE", SECRET)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert "Updated Coolify env NWN_MODULE" in caplog.text
    assert SECRET not in caplog.text


@pytest.mark.asyncio
async def test_patch_env_failure_logs_no_value(caplog):
    caplog.set_level(logging.INFO, logger="app.coolify")
    await _client(422).patch_env("svc", "NWN_MODULE", SECRET)
    assert "Failed to update Coolify env NWN_MODULE: HTTP 422" in caplog.text
    assert SECRET not in caplog.text
