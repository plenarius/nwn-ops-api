from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.server import (
    get_available_modules,
    get_module_name,
    get_module_version,
    get_player_count,
    get_server_status,
    get_staged_files,
)
from app.ssh import SshResult
from tests.conftest import make_settings


@pytest.mark.asyncio
async def test_get_server_status_healthy(mock_ssh):
    mock_ssh.run = AsyncMock(return_value=SshResult(0, 'running|Up 2 hours (healthy)', ''))
    result = await get_server_status(mock_ssh)
    assert result == "running:healthy"


@pytest.mark.asyncio
async def test_get_server_status_unreachable(mock_ssh):
    mock_ssh.run = AsyncMock(side_effect=Exception("timeout"))
    result = await get_server_status(mock_ssh)
    assert result is None


@pytest.mark.asyncio
async def test_get_player_count_from_the_database(mock_db_pool):
    conn = AsyncMock()
    cur = AsyncMock()
    cur.fetchone = AsyncMock(return_value=(5, 2))
    cur.__aenter__ = AsyncMock(return_value=cur)
    cur.__aexit__ = AsyncMock()
    conn.cursor = MagicMock(return_value=cur)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock()
    mock_db_pool.acquire = MagicMock(return_value=conn)

    s = make_settings(mysql_host="db", player_count_sql="SELECT 5, 2")
    result = await get_player_count(s, mock_db_pool)
    assert result == {"players": 5, "dms": 2}
    cur.execute.assert_awaited_once_with("SELECT 5, 2")


@pytest.mark.asyncio
async def test_get_player_count_from_the_status_query():
    with patch("app.services.server.nwn_query.query", new=AsyncMock(return_value={"players": 3, "max_players": 40, "module": "m"})) as q:
        assert await get_player_count(make_settings(nwn_port=5122)) == {"players": 3, "dms": 0}
    q.assert_awaited_once_with("10.0.0.2", 5122)
    with patch("app.services.server.nwn_query.query", new=AsyncMock(return_value=None)):
        assert await get_player_count(make_settings()) == {"players": -1, "dms": -1}


@pytest.mark.asyncio
async def test_get_module_version(mock_ssh):
    mock_ssh.run = AsyncMock(return_value=SshResult(0, 'MyWorld_6-17', ''))
    result = await get_module_version(mock_ssh, make_settings())
    assert result == "6.17"
    assert "/srv/game/.env" in mock_ssh.run.call_args[0][0]


@pytest.mark.asyncio
async def test_a_module_name_without_a_version_is_shown_whole(mock_ssh):
    mock_ssh.run = AsyncMock(return_value=SshResult(0, 'mymodule', ''))
    assert await get_module_version(mock_ssh, make_settings()) == "mymodule"


@pytest.mark.asyncio
async def test_module_name_falls_back_to_the_containers_environment(mock_ssh):
    async def run(cmd, timeout=15):
        if cmd.startswith("grep"):
            return SshResult(1, "", "")
        assert "docker inspect" in cmd and "grep '^NWN_MODULE='" in cmd   # only that line leaves the host
        return SshResult(0, "mymodule\n", "")
    mock_ssh.run = run
    assert await get_module_name(mock_ssh, make_settings()) == "mymodule"


@pytest.mark.asyncio
async def test_get_staged_files(mock_ssh):
    async def fake_run(cmd, timeout=15):
        if "modules" in cmd:
            return SshResult(0, "MyWorld_6-18.mod", "")
        if "hak" in cmd:
            return SshResult(0, "file1.hak\nfile2.hak", "")
        return SshResult(1, "", "")

    mock_ssh.run = fake_run
    mods, haks = await get_staged_files(mock_ssh, "/modules_stage", "/hak_stage")
    assert mods == ["MyWorld_6-18.mod"]
    assert len(haks) == 2


@pytest.mark.asyncio
async def test_available_modules_marks_staged_and_current(mock_ssh):
    async def run(cmd, timeout=15):
        if cmd.startswith("ls /srv/game/home/modules/.staged/*.mod"):
            return SshResult(0, "/srv/game/home/modules/.staged/w_2-0.mod", "")
        if cmd.startswith("grep"):
            return SshResult(0, "w_1-9", "")
        if cmd.startswith("ls -t"):
            return SshResult(0, "/srv/game/home/modules/w_1-9.mod", "")
        return SshResult(1, "", "")
    mock_ssh.run = run
    mods = await get_available_modules(mock_ssh, make_settings())
    assert mods == [{"name": "w_2-0", "file": "w_2-0.mod", "staged": True, "current": False},
                    {"name": "w_1-9", "file": "w_1-9.mod", "staged": False, "current": True}]


@pytest.mark.asyncio
@pytest.mark.parametrize("docker_line,expected", [
    ("running|Up 2 hours (healthy)", "running:healthy"),
    ("running|Up 2 hours (unhealthy)", "running:unhealthy"),
    ("running|Up 5 seconds (health: starting)", "running:starting"),
    ("running|Up 3 days", "running"),
    ("exited|Exited (0) 2 hours ago", "exited"),
    ("restarting|Restarting (1) 5 seconds ago", "restarting"),
])
async def test_get_server_status_reports_every_docker_health_state(mock_ssh, docker_line, expected):
    mock_ssh.run = AsyncMock(return_value=SshResult(0, docker_line, ''))
    assert await get_server_status(mock_ssh) == expected


def test_progress_marker_command_takes_the_latest_start():
    import asyncio
    import subprocess
    import tempfile

    from app.services import server
    log = "[a] Loading Module: Old\n[a] Primary server startup complete.\n[a] Area loading complete. Processed 5 areas.\n[b] Loading Module: New\n"
    captured = []

    class Ssh:
        async def run(self, cmd, timeout=15):
            captured.append(cmd)

            class R:
                stdout, returncode = "", 0
            return R()

    class Tasks:
        def running_tasks(self):
            return []

    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
        f.write(log)
    asyncio.run(server.get_progress(Ssh(), None, Tasks(), make_settings(log_file=f.name), 5))
    awk_cmd = next(c for c in captured if c.startswith("awk"))
    out = subprocess.run(awk_cmd, shell=True, capture_output=True, text=True).stdout.splitlines()
    assert out[0].endswith("Loading Module: New") and out[1] == "" and out[2] == ""
