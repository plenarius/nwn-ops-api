# Contracts between the game module and the ops API

These files describe what passes between an NWNX game server's module and this API: the Redis keys, the pub/sub messages and their JSON.
Names a server chooses (the Redis key prefix) are settings on both sides, and must be the same on both.

Versioning rule: a change that adds an optional field, a new command or a new reply message is compatible and only updates the file. A
change that renames or removes a key, field or command, or changes a field's type or meaning, is breaking: it must ship on both sides
together, and the contract file says at the top from which API version it applies.

- [restart.md](restart.md): the restart countdown, started and cancelled over NWNX_Redis pub/sub.
