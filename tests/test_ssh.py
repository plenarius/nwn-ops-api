import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ssh import SshResult, SshClient


@pytest.fixture
def ssh_client():
    return SshClient(host="10.0.0.2", username="nwn", key_path="/data/ssh/id_ed25519")


@pytest.mark.asyncio
async def test_ssh_result_fields():
    r = SshResult(returncode=0, stdout="hello", stderr="")
    assert r.returncode == 0
    assert r.stdout == "hello"
    assert r.stderr == ""


def _conn(exit_status=0, stdout=b"", stderr=b""):
    """A connection whose create_process() is an async context manager giving a process with those results, as asyncssh's is."""
    proc = MagicMock()
    proc.exit_status = exit_status
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=proc)
    cm.__aexit__ = AsyncMock(return_value=False)
    conn = MagicMock()
    conn.create_process = MagicMock(return_value=cm)
    return conn, proc


@pytest.mark.asyncio
async def test_ssh_cmd_success(ssh_client):
    conn, proc = _conn(0, b"output\n", b"")
    with patch.object(ssh_client, "_get_connection", AsyncMock(return_value=conn)):
        result = await ssh_client.run("echo hello", timeout=5)

    assert result.returncode == 0
    assert result.stdout == "output"
    conn.create_process.assert_called_once_with("echo hello")
    proc.communicate.assert_awaited_once_with(None)


@pytest.mark.asyncio
async def test_ssh_cmd_failure(ssh_client):
    conn, _ = _conn(1, b"", b"not found")
    with patch.object(ssh_client, "_get_connection", AsyncMock(return_value=conn)):
        result = await ssh_client.run("bad command", timeout=5)

    assert result.returncode == 1
    assert result.stderr == "not found"


@pytest.mark.asyncio
async def test_ssh_sends_input_and_decodes_cp1252_leniently(ssh_client):
    conn, proc = _conn(0, "Désirée".encode("cp1252"), b"")
    with patch.object(ssh_client, "_get_connection", AsyncMock(return_value=conn)):
        result = await ssh_client.run("cat", timeout=5, input=b"data")

    proc.communicate.assert_awaited_once_with(b"data")
    assert result.stdout.startswith("D") and result.stdout.endswith("sir\ufffde")  # not an exception, not truncated


@pytest.mark.asyncio
async def test_ssh_connects_when_there_is_no_connection(ssh_client):
    conn, _ = _conn(0, b"ok", b"")
    ssh_client._conn = None

    with patch("asyncssh.connect", new_callable=AsyncMock, return_value=conn) as mock_connect:
        result = await ssh_client.run("test", timeout=5)

    mock_connect.assert_called_once()
    assert result.stdout == "ok"


@pytest.mark.asyncio
async def test_ssh_reconnects_once_when_the_connection_dropped(ssh_client):
    import asyncssh

    dead = MagicMock()
    dead.create_process = MagicMock(side_effect=asyncssh.ConnectionLost("gone"))
    fresh, _ = _conn(0, b"ok", b"")
    ssh_client._conn = dead

    with patch("asyncssh.connect", new_callable=AsyncMock, return_value=fresh) as mock_connect:
        result = await ssh_client.run("test", timeout=5)

    mock_connect.assert_called_once()
    assert result.stdout == "ok"


@pytest.mark.asyncio
async def test_ssh_checks_the_host_key_when_given_a_known_hosts_file():
    client = SshClient(host="10.0.0.2", username="nwn", key_path="/k", known_hosts="/data/ssh/known_hosts")
    conn, _ = _conn(0, b"ok", b"")
    with patch("asyncssh.connect", new_callable=AsyncMock, return_value=conn) as mock_connect:
        await client.run("true", timeout=5)
    assert mock_connect.call_args.kwargs["known_hosts"] == "/data/ssh/known_hosts"
