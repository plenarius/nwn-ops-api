# Changelog

## 0.1.0 (unreleased)

The first version: the operations core of a private ops API that has run a persistent world's server since 2026, made generic.

- Status, modules, logs and progress of the game server; restart, cancel, stop and start; module, hak and full deploys; NWNX image pull
  and update; GitHub releases with generated notes and a repository overview; a self-description of every route and setting.
- Three key groups (full, admin, team); a known key on a route it can't use gets 403, a wrong key 401.
- Every part switches on from its settings; only `OPS_API_KEY` is required, and start-up contacts nothing.
- The restart countdown over NWNX_Redis pub/sub, with the prefix as a setting (docs/contracts/restart.md); without Redis, a restart from
  outside after the countdown.
- Coolify, or a start command over SSH, to bring the game back.
- Players counted with the game's own status query, or a query on the game database.
- Discord webhook announcements; optional OpenTelemetry tracing.
- An NWNX update leaves a game on another tag than `NWNX_IMAGE` alone instead of restarting it onto the same build; a full deploy leaves
  NWNX out for it.
- `POST /release` answers 502 with GitHub's reason, and `GET /repos` an error per repository, when GitHub refuses (they answered 500).
