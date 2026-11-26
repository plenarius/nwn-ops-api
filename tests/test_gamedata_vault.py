"""The server vault over SSH (with a fake SSH that answers like find, stat and base64 on the game host), and the vault name helpers."""

import base64
from unittest.mock import AsyncMock

import pytest

from app.gamedata import vault
from app.gamedata.vault_names import Ambiguous, display_name, is_raw, records, resolve
from app.ssh import SshResult
from tests.conftest import make_settings


def listed(raw: bytes) -> str:
    """A name as SshClient gives it with errors="surrogateescape"."""
    return raw.decode("utf-8", "surrogateescape")


def test_a_name_sent_back_finds_the_listed_name_it_reads_as():
    names = [listed(b"B\x84rbel"), "Elénwyn Fan", "O'Brien \"Ob\""]
    assert is_raw(names[0]) and not is_raw(names[1])
    assert display_name(names[0]) == "Bärbel" and display_name(listed(b"Caf\xe9")) == "Café"
    assert resolve(names, "Bärbel") == listed(b"B\x84rbel")
    assert resolve(names, "O'Brien \"Ob\"") == "O'Brien \"Ob\""
    assert resolve(names, "Barbel") is None
    with pytest.raises(Ambiguous):
        resolve([listed(b"caf\xe9"), "café"], "café")


def test_records_keep_tabs_and_newlines_inside_the_name():
    assert records("d\tone\0d\ttab\there\0f\tnew\nline\0", 2) == [["d", "one"], ["d", "tab\there"], ["f", "new\nline"]]
    assert records("", 2) == [] and records("broken\0", 2) == []


VAULT = "/srv/game/home/servervault"
BIC = b"BIC V3.2 made up"


def fake_ssh(files=None, fail=None):
    """{folder: {file: (size, mtime)}} as the game host would list them."""
    files = files if files is not None else {"ABCD1234": {"testa.bic": (100, 1700000000), "testa.bak": (90, 1690000000),
                                                          "other.bic": (50, 1710000000), "notes.txt": (1, 1)}}
    calls = []

    async def run(cmd, timeout=15, input=None, errors="replace"):
        calls.append(cmd)
        if fail and fail in cmd:
            return SshResult(1, "", "boom")
        if cmd.startswith(f"find {VAULT} "):
            return SshResult(0, "".join(f"d\t{d}\0" for d in files), "")
        for d, fs in files.items():
            if cmd.startswith(f"find {VAULT}/{d} "):
                if "%s" in cmd:
                    return SshResult(0, "".join(f"{s}\t{m}.5000000000\t{n}\0" for n, (s, m) in fs.items()), "")
                return SshResult(0, "".join(f"f\t{n}\0" for n in fs), "")
        if cmd.startswith("stat -c %Y"):
            return SshResult(0, "1700000000\n" + base64.b64encode(BIC).decode(), "")
        return SshResult(0, "", "")

    ssh = AsyncMock()
    ssh.run = AsyncMock(side_effect=run)
    ssh.calls = calls
    return ssh


@pytest.mark.asyncio
async def test_a_players_characters_newest_first():
    out = await vault.characters(fake_ssh(), VAULT, "ABCD1234")
    assert [c["file"] for c in out] == ["other", "testa"]
    assert out[1] == {"file": "testa", "size": 100, "saved_at": "2023-11-14T22:13:20+00:00"}


@pytest.mark.asyncio
async def test_unknown_folder_or_file_is_404_and_nothing_unlisted_reaches_a_command():
    ssh = fake_ssh()
    with pytest.raises(vault.VaultError) as e:
        await vault.characters(ssh, VAULT, "../etc")
    assert e.value.status == 404
    with pytest.raises(vault.VaultError) as e:
        await vault.read_character(ssh, VAULT, "ABCD1234", "nobody; rm -rf /")
    assert e.value.status == 404
    assert not any("rm -rf" in c or "../etc" in c for c in ssh.calls)


@pytest.mark.asyncio
async def test_read_a_character_file():
    ssh = fake_ssh()
    data, saved = await vault.read_character(ssh, VAULT, "ABCD1234", "testa")
    assert data == BIC and saved == "2023-11-14T22:13:20+00:00"
    assert ssh.calls[-1] == f"stat -c %Y {VAULT}/ABCD1234/testa.bic && base64 -w0 {VAULT}/ABCD1234/testa.bic"


@pytest.mark.asyncio
async def test_a_failed_listing_is_502():
    with pytest.raises(vault.VaultError) as e:
        await vault.characters(fake_ssh(fail="find"), VAULT, "ABCD1234")
    assert e.value.status == 502


@pytest.mark.asyncio
async def test_retire_dry_run_changes_nothing():
    ssh = fake_ssh()
    online = AsyncMock(return_value=False)
    out = await vault.retire(ssh, make_settings(), VAULT, "ABCD1234", "testa", dry_run=True, nobody_online=online, stamp="T1")
    assert out == {"dry_run": True, "folder": "ABCD1234", "file": "testa", "retired_as": "testa.bic.retired-T1", "also_bak": True}
    assert not any(c.startswith(("mv", "sudo")) for c in ssh.calls) and not online.called


@pytest.mark.asyncio
async def test_retire_is_refused_while_anyone_is_on():
    ssh = fake_ssh()
    with pytest.raises(vault.VaultError) as e:
        await vault.retire(ssh, make_settings(), VAULT, "ABCD1234", "testa", dry_run=False, nobody_online=AsyncMock(return_value=False))
    assert e.value.status == 409 and not any("mv" in c for c in ssh.calls)


@pytest.mark.asyncio
async def test_retire_renames_the_file_and_its_bak():
    ssh = fake_ssh()
    out = await vault.retire(ssh, make_settings(ssh_sudo=True), VAULT, "ABCD1234", "testa", dry_run=False,
                             nobody_online=AsyncMock(return_value=True), stamp="T1")
    assert out["dry_run"] is False
    d = f"{VAULT}/ABCD1234"
    assert ssh.calls[-1] == (f"sudo -n mv -n {d}/testa.bic {d}/testa.bic.retired-T1 && test ! -e {d}/testa.bic && test -e "
                             f"{d}/testa.bic.retired-T1 && sudo -n mv -n {d}/testa.bak {d}/testa.bak.retired-T1")


@pytest.mark.asyncio
async def test_a_failed_rename_is_502():
    with pytest.raises(vault.VaultError) as e:
        await vault.retire(fake_ssh(fail="mv -n"), make_settings(), VAULT, "ABCD1234", "other", dry_run=False,
                           nobody_online=AsyncMock(return_value=True))
    assert e.value.status == 502
