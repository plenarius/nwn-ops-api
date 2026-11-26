"""Game data for a website: talk table strings, 2DA rows, an item's examine panel, a character sheet from the server vault. Read-only and
nothing private (no log, address or account), so the read key opens them; taking a character out of the vault is the admin key's. The
vault routes also need GAME_VAULT=on: a vault folder's name is a player's CD key."""

from __future__ import annotations

import asyncio
import re
import struct

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.auth import require_feature
from app.gamedata import bic, gff, item_examine, vault
from app.gamedata.bic import item_name
from app.services import server

router = APIRouter(prefix="/api/v1/game", tags=["game"])
admin_router = APIRouter(prefix="/api/v1/game", tags=["game"])

_RESREF = re.compile(r"^[A-Za-z0-9_]{1,16}$")


def _resref(name: str, what: str) -> str:
    if not _RESREF.match(name):
        raise HTTPException(422, f"{what} must be 1 to 16 letters, digits or underscores")
    return name.lower()


async def _data(request: Request):
    gd = request.app.state.game_data
    await asyncio.to_thread(gd.ensure)
    return gd


async def _vault_call(coro):
    try:
        return await coro
    except vault.VaultError as e:
        raise HTTPException(e.status, e.detail)


@router.get("/tlk/{strref}", dependencies=[require_feature("game_data")])
async def tlk_string(request: Request, strref: int):
    """The text of a string reference: dialog.tlk below 16777216, the module's custom talk table from there."""
    gd = await _data(request)
    if strref < 0:
        raise HTTPException(422, "strref must be 0 or more")
    return {"strref": strref, "text": gd.strref(strref)}


@router.get("/2da/{table}", dependencies=[require_feature("game_data")])
async def twoda_table(request: Request, table: str):
    """A 2DA's columns and row count, and where the game finds it (a hak's name, module, override, ovr or base)."""
    name = _resref(table, "table")
    gd = await _data(request)
    t = await asyncio.to_thread(gd.twoda, name)
    if t is None:
        raise HTTPException(404, f"No {name}.2da in the game data")
    return {"table": name, "from": gd.source(name, "2da"), "columns": t.columns, "rows": t.row_count}


@router.get("/2da/{table}/{row}", dependencies=[require_feature("game_data")])
async def twoda_row(request: Request, table: str, row: int, names: bool = False):
    """One row of a 2DA: {column: cell} (null for ****). With names=true, also the talk table text of the row's name columns (Name,
    Description, and the like, whatever holds a string reference)."""
    name = _resref(table, "table")
    gd = await _data(request)
    t = await asyncio.to_thread(gd.twoda, name)
    if t is None:
        raise HTTPException(404, f"No {name}.2da in the game data")
    cells = t.row(row)
    if cells is None:
        raise HTTPException(404, f"{name}.2da has {t.row_count} rows")
    out = {"table": name, "row": row, "from": gd.source(name, "2da"), "columns": cells}
    if names:
        out["text"] = {c: gd.strref(v) for c, v in cells.items() if c.lower() in STRREF_COLUMNS and v and v.lstrip("-").isdigit()
                       and gd.strref(v)}
    return out


# Columns that hold string references in the stock 2DAs (lower case).
STRREF_COLUMNS = {"name", "description", "descriptions", "feat", "strref", "gamestrref", "namelower", "nameplural", "convername",
                  "convernamelower", "baseitemstatref", "short", "tooltip", "spelldesc"}


@router.get("/items/{resref}", dependencies=[require_feature("game_data")])
async def item(request: Request, resref: str, identified: bool = True):
    """An item blueprint (.uti from the haks, the module or the base game) as the game's examine panel words it: its name, kind, weight,
    value, required level and properties. identified=false shows it as a player sees it unidentified."""
    name = _resref(resref, "resref")
    gd = await _data(request)

    def build():
        doc = gd.blueprint(name)
        if doc is None:
            return None
        if not identified:
            doc["Identified"] = {"type": "byte", "value": 0}
        t = gd.tables
        return {
            "resref": name,
            "from": gd.source(name, "uti"),
            "name": item_examine.shown_name(doc, item_name(doc, gd.strref), t, gd.strref),
            "tag": str(gff._v(doc.get("Tag")) or ""),
            "base_item": {"id": int(gff._v(doc.get("BaseItem")) or 0), "name": item_examine.kind_name(doc, t, gd.strref)},
            "examine": item_examine.examine(doc, t, gd.strref, gd.max_single(), ilr=gd.ilr),
        }

    try:
        out = await asyncio.to_thread(build)
    except (ValueError, KeyError, struct.error) as e:
        raise HTTPException(422, f"Could not read {name}.uti: {type(e).__name__}")
    if out is None:
        raise HTTPException(404, f"No {name}.uti in the game data")
    return out


@router.get("/vault/{folder}", dependencies=[require_feature("vault")])
async def vault_folder(request: Request, folder: str):
    """The character files in one player's server vault folder: [{file, size, saved_at}], newest first."""
    s = request.app.state
    return {"folder": folder, "characters": await _vault_call(vault.characters(s.ssh, s.settings.servervault, folder))}


@router.get("/vault/{folder}/{file}", dependencies=[require_feature("vault", "game_data")])
async def character_sheet(request: Request, folder: str, file: str):
    """A character's sheet from its server vault file: name, description, race, alignment, classes and levels, ability scores, skill
    ranks, feats, the gear worn (with each item's examine panel), the portrait resref, when the game last saved it."""
    s = request.app.state
    data, saved_at = await _vault_call(vault.read_character(s.ssh, s.settings.servervault, folder, file))
    gd = await _data(request)

    def build():
        doc = gff.read(data)
        if doc.get("__data_type", "").strip() != "BIC":
            raise ValueError("not a character file")
        return bic.sheet(doc, gd)

    try:
        out = await asyncio.to_thread(build)
    except (ValueError, KeyError, IndexError, struct.error) as e:
        raise HTTPException(422, f"Could not read the character file: {e if isinstance(e, ValueError) else type(e).__name__}")
    return {"folder": folder, "file": file, "saved_at": saved_at, **out}


class RetireBody(BaseModel):
    dry_run: bool = True


@admin_router.post("/vault/{folder}/{file}/retire", dependencies=[require_feature("vault")])
async def retire_character(request: Request, folder: str, file: str, body: RetireBody | None = None):
    """Take a character out of a player's vault without deleting it: the file is renamed to <file>.bic.retired-<time> (and its .bak).
    Refused while anyone is online. dry_run (the default) only says what would happen."""
    s = request.app.state
    body = body or RetireBody()

    async def nobody_online() -> bool:
        if not server.is_running(await server.get_server_status(s.ssh, s.settings.container_filter)):
            return True
        counts = await server.get_player_count(s.settings, s.db_pool)
        return counts["players"] == 0 and counts["dms"] == 0

    return await _vault_call(vault.retire(s.ssh, s.settings, s.settings.servervault, folder, file, dry_run=body.dry_run,
                                          nobody_online=nobody_online))
