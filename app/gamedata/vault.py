"""The server vault on the game host, over SSH: a player's folder, one character file, and taking a character out of the vault.

A folder or file named by a caller is never put into a command as given: it is looked up in a listing of the vault (vault_names.resolve),
so only names that are there, byte for byte, reach the shell, and always quoted.
"""

from __future__ import annotations

import base64
import logging
import shlex
from datetime import datetime, timezone
from typing import Optional

from app.gamedata.vault_names import Ambiguous, display_name, records, resolve

logger = logging.getLogger(__name__)

RETIRED_SUFFIX = ".retired-"
MAX_BIC_BYTES = 32 * 1024 * 1024


class VaultError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def _iso(epoch: str) -> Optional[str]:
    return datetime.fromtimestamp(int(epoch), timezone.utc).isoformat() if epoch.isdigit() else None


async def _listing(ssh, path: str, kind: str, fields: str) -> list[list[str]]:
    """find's records of one folder's entries of this type (d or f): `fields` then the name, tab separated."""
    q = shlex.quote(path)
    n = fields.count("\\t") + 1
    res = await ssh.run(f"find {q} -mindepth 1 -maxdepth 1 -type {kind} -printf '{fields}\\0'", timeout=20, errors="surrogateescape")
    if res.returncode not in (0, None):
        raise VaultError(502, "Could not list the server vault on the game host")
    return records(res.stdout, n)


async def folder_path(ssh, vault: str, folder: str) -> str:
    """The path of the player's folder `folder` (its display name) in the vault. VaultError 404 if there is none."""
    names = [r[1] for r in await _listing(ssh, vault, "d", "d\\t%f")]
    try:
        real = resolve(names, folder)
    except Ambiguous:
        raise VaultError(409, "More than one vault folder has that name")
    if real is None:
        raise VaultError(404, "No such vault folder")
    return f"{vault.rstrip('/')}/{real}"


async def characters(ssh, vault: str, folder: str) -> list[dict]:
    """The character files in one player's folder: [{file, size, saved_at}], `file` without `.bic`, newest first."""
    path = await folder_path(ssh, vault, folder)
    out = []
    for size, mtime, name in await _listing(ssh, path, "f", "%s\\t%T@\\t%f"):
        if name.lower().endswith(".bic"):
            out.append({"file": display_name(name[:-4]), "size": int(size) if size.isdigit() else None,
                        "saved_at": _iso(mtime.split(".")[0])})
    return sorted(out, key=lambda c: c["saved_at"] or "", reverse=True)


async def character_path(ssh, vault: str, folder: str, file: str) -> tuple[str, list[str]]:
    """(path of `<file>.bic` in the folder, every file name in the folder). VaultError 404 if it is not there."""
    path = await folder_path(ssh, vault, folder)
    names = [r[1] for r in await _listing(ssh, path, "f", "f\\t%f")]
    try:
        real = resolve([n[:-4] for n in names if n.lower().endswith(".bic")], file)
    except Ambiguous:
        raise VaultError(409, "More than one character file has that name")
    if real is None:
        raise VaultError(404, "No such character file")
    return f"{path}/{real}.bic", names


async def read_character(ssh, vault: str, folder: str, file: str) -> tuple[bytes, Optional[str]]:
    """The character file's bytes and when the game last saved it."""
    path, _ = await character_path(ssh, vault, folder, file)
    q = shlex.quote(path)
    res = await ssh.run(f"stat -c %Y {q} && base64 -w0 {q}", timeout=60, errors="surrogateescape")
    lines = res.stdout.split("\n", 1)
    if res.returncode not in (0, None) or len(lines) < 2:
        raise VaultError(502, "Could not read the character file on the game host")
    if len(lines[1]) > MAX_BIC_BYTES * 4 // 3 + 4:
        raise VaultError(413, "The character file is too large")
    return base64.b64decode(lines[1].strip()), _iso(lines[0].strip())


def retired_name(filename: str, stamp: str) -> str:
    return f"{filename}{RETIRED_SUFFIX}{stamp}"


async def retire(ssh, settings, vault: str, folder: str, file: str, *, dry_run: bool, nobody_online, stamp: Optional[str] = None) -> dict:
    """Take a character out of the player's vault without deleting anything: `<file>.bic` is renamed in its folder to
    `<file>.bic.retired-<UTC time>` (and the game's `<file>.bak` beside it, if any). The game lists only `.bic` files, so the character is
    gone from the player's list; renaming it back restores it.

    The rename is refused while anyone is on the server (`nobody_online`, an async callable): a character who is logged in would be saved
    again under its old name. A dry run only says what would happen."""
    path, names = await character_path(ssh, vault, folder, file)
    folder_path_ = path.rsplit("/", 1)[0]
    stem = path.rsplit("/", 1)[1][:-4]
    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bak = next((n for n in names if n == f"{stem}.bak"), None)
    result = {"dry_run": dry_run, "folder": folder, "file": file, "retired_as": display_name(retired_name(f"{stem}.bic", stamp)),
              "also_bak": bak is not None}
    if dry_run:
        return result
    if not await nobody_online():
        raise VaultError(409, "Someone is on the server (or the player count is unknown): retire characters while nobody is online")
    q, sudo = shlex.quote, settings.sudo
    new = f"{folder_path_}/{retired_name(stem + '.bic', stamp)}"
    cmd = f"{sudo}mv -n {q(path)} {q(new)} && test ! -e {q(path)} && test -e {q(new)}"
    if bak:
        cmd += f" && {sudo}mv -n {q(folder_path_ + '/' + bak)} {q(folder_path_ + '/' + retired_name(bak, stamp))}"
    res = await ssh.run(cmd, timeout=30)
    if res.returncode not in (0, None):
        raise VaultError(502, "The rename failed on the game host; check the vault folder by hand")
    logger.info("retired a character file in the server vault")
    return result
