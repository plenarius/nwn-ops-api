from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import Settings

API_KEY = "test-full-key-0123456789"
SETTING_VARS = [n.upper() for n in Settings.model_fields] + ["COOLIFY_API_URL"]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every test starts from no settings at all, whatever the machine running it has set."""
    for name in SETTING_VARS:
        monkeypatch.delenv(name, raising=False)


def make_settings(**kw) -> Settings:
    base = {"ops_api_key": API_KEY, "game_host": "10.0.0.2", "game_dir": "/srv/game", "container_filter": "nwserver"}
    base.update(kw)
    return Settings(**base)


@pytest.fixture
def settings():
    return make_settings(coolify_url="https://coolify.test", coolify_token="t", coolify_service_uuid="test-uuid")


@pytest.fixture
def mock_ssh():
    return AsyncMock()


@pytest.fixture
def mock_redis():
    r = AsyncMock()
    r.get = AsyncMock(return_value=None)
    r.set = AsyncMock()
    r.delete = AsyncMock()
    r.publish = AsyncMock()
    return r


@pytest.fixture
def mock_db_pool():
    pool = MagicMock()
    conn = AsyncMock()
    cursor = AsyncMock()
    cursor.fetchone = AsyncMock(return_value=(0,))
    cursor.fetchall = AsyncMock(return_value=[])
    cursor.__aenter__ = AsyncMock(return_value=cursor)
    cursor.__aexit__ = AsyncMock()
    conn.cursor = MagicMock(return_value=cursor)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock()
    pool.acquire = MagicMock(return_value=conn)
    return pool


@pytest.fixture
def mock_coolify():
    return AsyncMock()
