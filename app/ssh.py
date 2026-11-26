import asyncio
import logging
from dataclasses import dataclass

import asyncssh

from app import tracing

logger = logging.getLogger(__name__)


@dataclass
class SshResult:
    returncode: int
    stdout: str
    stderr: str


class SshClient:
    def __init__(self, host: str, username: str, key_path: str, known_hosts: str = ""):
        self._host = host
        self._username = username
        self._key_path = key_path
        # A known_hosts file to check the host's key against; "" accepts any key (main.py warns at start-up).
        self._known_hosts = known_hosts or None
        self._conn: asyncssh.SSHClientConnection | None = None
        self._lock = asyncio.Lock()

    async def _get_connection(self) -> asyncssh.SSHClientConnection:
        if self._conn is not None:
            return self._conn

        async with self._lock:
            if self._conn is not None:
                return self._conn
            logger.info("Connecting to %s@%s", self._username, self._host)
            # encoding=None keeps the channel in raw bytes, so a file name that is not UTF-8 (an old vault entry) doesn't cut the
            # output short. Each call decodes (run's `errors`).
            self._conn = await asyncssh.connect(
                self._host,
                username=self._username,
                client_keys=[self._key_path],
                known_hosts=self._known_hosts,
                encoding=None,
            )
            return self._conn

    @staticmethod
    def _to_text(data, errors: str = "replace") -> str:
        if data is None:
            return ""
        if isinstance(data, bytes):
            return data.decode("utf-8", errors=errors).strip()
        return data.strip()

    async def _exec(self, cmd: str, timeout: int, input: bytes | None, errors: str) -> SshResult:
        # A span names the program only (the first word): the arguments name files and characters.
        with tracing.span(f"ssh {_program(cmd)}", **{"server.address": self._host}) as span:
            r = await self._exec_untraced(cmd, timeout, input, errors)
            if span is not None:
                span.set_attribute("process.exit.code", r.returncode if r.returncode is not None else -1)
            return r

    async def _exec_untraced(self, cmd: str, timeout: int, input: bytes | None, errors: str) -> SshResult:
        conn = await self._get_connection()
        command: str | bytes = cmd
        if any("\udc80" <= ch <= "\udcff" for ch in cmd):
            # A command built from output read with errors="surrogateescape": send the exact bytes it listed (a file name that is not UTF-8).
            command = cmd.encode("utf-8", "surrogateescape")
        async with conn.create_process(command) as proc:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input), timeout=timeout,
            )
            return SshResult(
                returncode=proc.exit_status,
                stdout=self._to_text(stdout, errors),
                stderr=self._to_text(stderr, errors),
            )

    async def run(self, cmd: str, timeout: int = 15, input: bytes | None = None, errors: str = "replace") -> SshResult:
        """errors: how output that is not UTF-8 is decoded. "surrogateescape" keeps every byte (a file name in a legacy encoding), so the text
        can go back into a command and name the same file; "replace" (the default) is for output that is only shown."""
        try:
            return await self._exec(cmd, timeout, input, errors)
        except (asyncssh.Error, OSError) as e:
            logger.warning("SSH connection failed, reconnecting: %s", e)
            self._conn = None
            return await self._exec(cmd, timeout, input, errors)

    async def fetch(self, remote: str, local) -> None:
        """Copies one file from the host (SFTP)."""
        conn = await self._get_connection()
        async with conn.start_sftp_client() as sftp:
            await sftp.get(remote, str(local))

    async def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None


def _program(cmd: str) -> str:
    """The command's program for a span name: "docker" for "docker exec ...", "cat" for "cd /x && cat y", "sh" when it can't tell."""
    words = cmd.replace("&&", " ").replace(";", " ").split()
    while words and (words[0] in ("cd", "sudo", "nice", "timeout", "env") or "=" in words[0] or words[0].startswith("-")):
        # cd's folder, timeout's seconds: skip the word after them too
        w = words.pop(0)
        if w in ("cd", "timeout") and words:
            words.pop(0)
    return (words[0].rsplit("/", 1)[-1][:40] if words else "sh") or "sh"
