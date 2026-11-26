"""Announcements to a Discord channel through a webhook (DISCORD_WEBHOOK_URL).

`send` never raises: a notice that fails is logged (the HTTP status, or the exception's type) and the restart or deploy that sent it carries
on. The webhook URL holds its token, so it is never logged. Unset, a notice is only logged.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import aiohttp

logger = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=10)
ORANGE, GREEN, BLUE, RED = 0xF59E42, 0x43B581, 0x5865F2, 0xED4245

# kind -> (title, colour)
KINDS = {
    "server.restart_scheduled": ("Server restart scheduled", ORANGE),
    "server.stopping": ("Server shutting down", RED),
    "server.restart_cancelled": ("Server restart cancelled", GREEN),
    "server.update_incoming": ("Server update incoming", ORANGE),
    "server.restarting": ("Server restarting", BLUE),
    "server.updated": ("Server updated", BLUE),
    "nwnx.update": ("NWNX update", ORANGE),
    "nwnx.updated": ("NWNX updated", BLUE),
}


def minutes(n: int) -> str:
    return f"{n} minute{'s' if n != 1 else ''}"


def payload(settings, kind: str, text: str, version: str | None = None, now: datetime | None = None) -> dict:
    title, colour = KINDS.get(kind, (kind, BLUE))
    embed = {"title": title, "description": text[:4000], "color": colour,
             "timestamp": (now or datetime.now(timezone.utc)).isoformat()}
    if version:
        embed["footer"] = {"text": f"Version {version}"[:200]}
    out = {"username": settings.discord_username or "Server", "embeds": [embed]}
    if settings.discord_avatar_url:
        out["avatar_url"] = settings.discord_avatar_url
    return out


async def send(settings, session: aiohttp.ClientSession | None, kind: str, text: str, *, version: str | None = None) -> bool:
    """Posts one notice. True when Discord took it. Never raises."""
    try:
        url = getattr(settings, "discord_webhook_url", "") if settings is not None else ""
        if not url:
            logger.info("notice %s (not sent, DISCORD_WEBHOOK_URL unset): %s", kind, text)
            return False
        own = session is None or session.closed
        if own:
            session = aiohttp.ClientSession()
        try:
            async with session.post(url, json=payload(settings, kind, text, version), timeout=TIMEOUT) as resp:
                if resp.status in (200, 204):
                    return True
                logger.error("notice %s: Discord answered %d", kind, resp.status)
                return False
        finally:
            if own:
                await session.close()
    except Exception as e:  # the type only: never anything that could carry the URL
        logger.error("notice %s: the post to Discord failed (%s)", kind, type(e).__name__)
        return False
