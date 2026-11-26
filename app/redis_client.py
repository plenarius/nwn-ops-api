"""Commands to the running game over NWNX_Redis pub/sub (docs/contracts/restart.md).

The API publishes {"cmd", "request_id", "args"} on <prefix>cmd and reads the game's reply from <prefix>response:<request_id>. The prefix is
REDIS_PREFIX, the same value the module uses. Every function takes client None (Redis not configured) and then does nothing.
"""

import asyncio
import json
import logging
import uuid

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

NO_REDIS = "Redis is not configured (REDIS_URL)."


def create_redis(url: str) -> aioredis.Redis | None:
    """Lazy like every redis-py client: nothing connects until the first command, so an unreachable Redis never stops the API starting."""
    if not url:
        return None
    return aioredis.from_url(url, decode_responses=True, socket_connect_timeout=3, socket_timeout=5)


async def redis_request(client, cmd: str, args: dict, prefix: str = "nwn:", timeout: float = 5.0) -> dict | None:
    """Publish a command and wait for the game's whole answer ({status, message} and, for some commands, data).

    Returns the answer, or None when the game did not answer within `timeout` (or there is no Redis)."""
    if client is None:
        return None
    request_id = str(uuid.uuid4())
    payload = json.dumps({"cmd": cmd, "request_id": request_id, "args": args})
    await client.publish(f"{prefix}cmd", payload)

    response_key = f"{prefix}response:{request_id}"
    result = None
    for _ in range(max(1, int(timeout / 0.2))):
        result = await client.get(response_key)
        if result:
            break
        await asyncio.sleep(0.2)

    if not result:
        return None
    data = json.loads(result)
    return data if isinstance(data, dict) else {"status": "error", "message": "The game's answer was not an object."}


async def redis_command(client, cmd: str, args: dict, prefix: str = "nwn:", timeout: float = 5.0) -> tuple[bool, str]:
    """Publish a command and wait for a response. Returns (success, message)."""
    if client is None:
        return False, NO_REDIS
    try:
        data = await redis_request(client, cmd, args, prefix, timeout)
    except Exception as e:  # noqa: BLE001 - an unreachable Redis is an answer too
        logger.warning("Redis command %s failed: %s", cmd, type(e).__name__)
        return False, f"Redis unreachable ({type(e).__name__})."
    if data is None:
        return False, "No response from game server."
    return data.get("status") == "ok", data.get("message", "")


async def set_restart_pending(client, reason: str, seconds: int, started_at: str, prefix: str = "nwn:") -> None:
    """Store the pending restart for dashboards (the game neither reads nor writes it)."""
    if client is None:
        return
    info = json.dumps({"reason": reason, "seconds": seconds, "started_at": started_at})
    try:
        await client.set(f"{prefix}restart_pending", info, ex=seconds + 60)
    except Exception as e:  # noqa: BLE001
        logger.warning("restart_pending not stored: %s", type(e).__name__)


async def clear_restart_pending(client, prefix: str = "nwn:") -> None:
    if client is None:
        return
    try:
        await client.delete(f"{prefix}restart_pending")
    except Exception as e:  # noqa: BLE001
        logger.warning("restart_pending not cleared: %s", type(e).__name__)


async def get_restart_pending(client, prefix: str = "nwn:") -> dict | None:
    if client is None:
        return None
    raw = await client.get(f"{prefix}restart_pending")
    if raw:
        return json.loads(raw)
    return None
