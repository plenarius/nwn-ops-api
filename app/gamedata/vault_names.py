"""File and folder names from the server vault on the game host, read so that any name comes back byte for byte.

A listing is run with `SshClient.run(..., errors="surrogateescape")` and NUL-terminated records (`find -printf '...\\0'`): every byte of a name
survives, including bytes that are not UTF-8 (an old vault copied from Windows can have names in code page 437 or Windows-1252), quotes,
tabs and newlines. Such a
name, put back into a command, names the same file (SshClient sends a command holding one as its exact bytes). It can't go into a database or
a JSON response as it is: `display_name` gives the readable text to store and show, and `resolve` finds the listed name again from that text.
"""

from __future__ import annotations

from typing import Iterable


def is_raw(name: str) -> bool:
    """True if the name holds bytes that are not UTF-8 (read with surrogateescape): it can name the file in a command but not be shown."""
    return any("\udc80" <= ch <= "\udcff" for ch in name)


def display_name(name: str) -> str:
    """A folder or file name from the vault listing, read with surrogateescape, as text that can be stored and shown. UTF-8 names come back as
    they are. Names in single-byte encodings whose accented bytes all fall in 0x80-0xA5 read as code page 437 (a zip of a Windows folder:
    0x82 'é', 0x84 'ä'), the rest as Windows-1252 (0xE9 'é'). The same bytes always give the same text, so a name sent back finds its file."""
    if not is_raw(name):
        return name
    raw = name.encode("utf-8", "surrogateescape")
    high = [b for b in raw if b >= 0x80]
    codec = "cp437" if all(b <= 0xA5 for b in high) else "cp1252"
    return raw.decode(codec, errors="replace")


def records(stdout: str, fields: int = 1) -> list[list[str]]:
    """The NUL-terminated records of a `find -printf` listing, each split on tabs into `fields` parts (the last part, the name, may itself
    hold tabs). Records with fewer parts are dropped. Put the name last, after a field such as `%y` or `%s`: SshClient strips the output, which
    would otherwise eat the leading spaces of the first name."""
    out = []
    for record in stdout.split("\0"):
        if not record:
            continue
        parts = record.split("\t", fields - 1)
        if len(parts) == fields:
            out.append(parts)
    return out


class Ambiguous(LookupError):
    """More than one listed name reads as the given text (the same name in two encodings)."""


def resolve(listed: Iterable[str], shown: str) -> str | None:
    """The listed name (as read with surrogateescape) that `shown` stands for: the name a client got from `display_name` and sent back. For a
    UTF-8 or ASCII name this is the name itself. None if no listed name reads as `shown`; Ambiguous if several do."""
    hits = [n for n in listed if display_name(n) == shown]
    if len(hits) > 1:
        raise Ambiguous(shown)
    return hits[0] if hits else None
