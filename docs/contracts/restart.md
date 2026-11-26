# Restart countdown

Applies from nwn-ops-api 0.1.0.

The API asks the game to restart after a countdown, or to cancel the countdown, over NWNX_Redis pub/sub. The game announces the countdown to
players, and at zero it shuts the server down. Starting the server again is the host's job (the container's restart policy), not the
game's.

Without Redis (`REDIS_URL` unset) none of this is used: the API waits the countdown itself and then restarts the container, and players get
no warning in game (only the Discord notice, if a webhook is set).

## Settings

| Setting | Meaning | Default |
|---|---|---|
| prefix | Shared by both sides: `REDIS_PREFIX` in the API, the same string in the module. | `nwn:` |
| command channel | The pub/sub channel the game listens on. | `<prefix>cmd` |
| reply key | Where the game stores its reply to one request. | `<prefix>response:<request_id>` |
| reply wait | How long the API waits for the reply (`REDIS_REPLY_TIMEOUT`). | 5 seconds |

## Subscribing the server

The NWNX_Redis plugin subscribes the server, from its environment:

```
NWNX_REDIS_SKIP=n
NWNX_REDIS_HOST=<redis host>
NWNX_REDIS_PORT=6379
NWNX_REDIS_PUBSUB_SCRIPT=on_pubsub      # the module's script NWNX_Redis runs for each message
NWNX_REDIS_PUBSUB_CHANNELS=nwn:cmd      # the command channel
```

The pub/sub script should ignore messages on any other channel. Other commands may share the channel; they are not part of this contract.

## The message

The API publishes one JSON object on the command channel:

| Field | Type | Required | Meaning |
|---|---|---|---|
| `cmd` | string | yes | `restart` or `cancel_restart`. |
| `request_id` | string | no | Where to put the reply. The API sends a UUID4. With no `request_id` (or `""`) the game does the command and stores no reply. |
| `args` | object | yes for `restart` | The command's arguments. `cancel_restart` reads none; the API sends `{}`. |

`args` for `restart`:

| Field | Type | Required | Meaning |
|---|---|---|---|
| `seconds` | integer | yes | Length of the countdown. The API sends at least 60 from its restart and stop routes. 0 means shut down now. |
| `reason` | string | no | Shown to players in the first announcement. |

```json
{"cmd": "restart", "request_id": "3f1c…", "args": {"seconds": 300, "reason": "manual"}}
{"cmd": "cancel_restart", "request_id": "9a2e…", "args": {}}
```

The API sends these reasons: what the caller of `POST /api/v1/server/restart` or `/stop` gave (`manual` and `maintenance` by default),
`deploy v<version> by <requester>` (the module's name instead of `v<version>` when it holds no version), `full-deploy` and `nwnx-update`.

A message that is not a JSON object should be logged and get no reply. An unknown `cmd` should get the reply `error`,
`Unknown command: <cmd>`.

## The reply

The reply is not published. The game stores it as a plain Redis string under the reply key, with a short time to live (30 seconds is
enough: the API reads it within its reply wait):

```json
{"status": "ok" | "error", "message": "<text>"}
```

The API reads that key every 0.2 seconds for up to the reply wait. With no reply in that time it treats the command as failed, with the
message `No response from game server.`

Replies to `restart`:

| Case | status | message (suggested) |
|---|---|---|
| Started | `ok` | `Restart countdown started (<seconds>s, <reason>)` |
| A countdown is already running | `error` | `A restart is already in progress.` The running countdown goes on. |

Replies to `cancel_restart`:

| Case | status | message (suggested) |
|---|---|---|
| Cancelled | `ok` | `Restart cancelled.` |
| No countdown running | `error` | `No restart is in progress.` |
| The countdown reached zero | `error` | `Too late to cancel: the server is shutting down.` Nothing is cancelled. |

The API passes the message on to its caller unchanged (`POST /api/v1/server/cancel-restart` answers `{"ok", "message"}`).

## What players should see

Up to the module. A countdown that works well: a server message at the start (with the reason), then at 5-minute marks, at 2 minutes,
1 minute, 30 seconds and 10 seconds, an on-screen timer, and at zero a last message, a save of whatever must not be lost, and
`NWNX_Administration_ShutdownServer` two seconds later. A cancel tells everyone it was cancelled.

## The API side

How this API uses the contract. Not part of what the game needs.

| Method and path | Keys | Body | Answer |
|---|---|---|---|
| `POST /api/v1/server/restart` | full, admin or team | `{"wait_minutes": int = 5, "reason": str = "manual"}` | 200 `{"ok": true, "message": "Restart countdown started (<seconds>s)"}`. 409 when a restart or deploy is already running. |
| `POST /api/v1/server/stop` | full or admin | `{"wait_minutes": int = 5, "reason": str = "maintenance"}` | 200 `{"ok": true, "message": "Stop countdown started (<seconds>s)"}`. 409 when a stop or deploy is already running. |
| `POST /api/v1/server/cancel-restart` | full, admin or team | none | 200 `{"ok": bool, "message": "<the game's message>"}` |

- `restart` and `stop` clamp `wait_minutes` to 0..30 and send at least 60 seconds.
- Both answer at once. The command is sent in a background task, so the HTTP answer does not carry the game's reply; the API's log does.
- When the game does not reply `ok` and the server is not running, `restart` starts it (Coolify, or `GAME_START_COMMAND`).
- `stop` sends the same `restart` command. After the countdown plus 15 seconds, it stops the container over SSH.
- The API keeps its own key `<prefix>restart_pending` (`{"reason", "seconds", "started_at"}`, time to live seconds + 60) for dashboards
  (`GET /api/v1/server/status` and `/progress` read it). The game neither reads nor writes it.

## Known gaps

1. A refused `restart` still shows as pending after a full deploy or an NWNX update: those write `<prefix>restart_pending` whatever the
   game replied. The restart route writes it only on `ok`.
2. A refused cancel clears the pending record, even when the game answered "too late".
3. A reply that comes after the reply wait is lost to the API.
