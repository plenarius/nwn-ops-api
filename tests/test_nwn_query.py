import asyncio
import struct

import pytest

from app import nwn_query


def _bnxr(players, max_players, module):
    m = module.encode()
    return b"BNXR" + struct.pack("<H", 5121) + bytes([0xFC, 0, 1, 40, players, max_players, 0, 1, 0, 0, 1, 0, 3, len(m)]) + m


def test_parse_bnxr():
    assert nwn_query.parse_bnxr(_bnxr(3, 40, "mymodule")) == {"players": 3, "max_players": 40, "module": "mymodule"}
    assert nwn_query.parse_bnxr(b"BNXR\x01") is None
    assert nwn_query.parse_bnxr(b"XXXX" + bytes(30)) is None


@pytest.mark.asyncio
async def test_query_a_server_on_this_machine():
    loop = asyncio.get_running_loop()

    class Server(asyncio.DatagramProtocol):
        def connection_made(self, t):
            self.t = t

        def datagram_received(self, data, addr):
            assert data[:4] == b"BNXI"
            self.t.sendto(_bnxr(2, 10, "test"), addr)
    transport, _ = await loop.create_datagram_endpoint(Server, local_addr=("127.0.0.1", 0))
    port = transport.get_extra_info("sockname")[1]
    try:
        assert await nwn_query.query("127.0.0.1", port) == {"players": 2, "max_players": 10, "module": "test"}
    finally:
        transport.close()


@pytest.mark.asyncio
async def test_query_nobody_there():
    assert await nwn_query.query("127.0.0.1", 9, timeout=0.3) is None
