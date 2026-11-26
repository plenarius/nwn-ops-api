"""The status query a player's game sends (BNXI), answered with BNXR: the module, the players online and the player limit."""

import asyncio
import struct


class _Proto(asyncio.DatagramProtocol):
    def __init__(self):
        self.answer: asyncio.Future = asyncio.get_running_loop().create_future()

    def datagram_received(self, data, addr):
        if not self.answer.done():
            self.answer.set_result(data)

    def error_received(self, exc):
        if not self.answer.done():
            self.answer.set_exception(exc)


def parse_bnxr(reply: bytes) -> dict | None:
    """{players, max_players, module} from a BNXR answer, or None for anything else."""
    if reply[:4] != b"BNXR" or len(reply) < 20 or len(reply) < 20 + reply[19]:
        return None
    return {"players": reply[10], "max_players": reply[11], "module": reply[20:20 + reply[19]].decode("utf-8", "replace")}


async def query(host: str, port: int = 5121, timeout: float = 3.0) -> dict | None:
    """The server's answer, or None when nothing (or something else) answers within `timeout`."""
    loop = asyncio.get_running_loop()
    try:
        transport, proto = await loop.create_datagram_endpoint(_Proto, remote_addr=(host, port))
    except OSError:
        return None
    try:
        transport.sendto(b"BNXI" + struct.pack("<H", port))
        return parse_bnxr(await asyncio.wait_for(proto.answer, timeout))
    except (asyncio.TimeoutError, OSError):
        return None
    finally:
        transport.close()
