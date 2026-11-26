# nwn-ops-api

A small HTTP API that operates a Neverwinter Nights: Enhanced Edition server running NWNX in Docker: its status, restart, stop and start,
module and hak deploys, NWNX image updates, and GitHub releases. A Discord bot, a website or a dashboard talks to it with a key, instead of
each holding SSH access to the game host.

It is written for the setup in two hosts: a control host (with Coolify, optionally) and a game host, joined by a private network such as
WireGuard. The API runs on the control host and reaches the game host over SSH; it never needs a Docker socket.

## What it does

| Route | Key | What |
|---|---|---|
| `GET /health` | none | For Docker's health check |
| `GET /api/v1/server/status` | read | Running or not, when it last started (`started_at`, ISO 8601 UTC, null if unknown), players online, the module, the NWNX build running and the newest one, what is staged, a pending countdown |
| `GET /api/v1/server/modules` | read | The modules that could be deployed (staged and live), with the current one |
| `GET /api/v1/server/logs?lines=20` | team | The end of the game's log |
| `GET /api/v1/server/progress` | read | What is happening right now: running operations, the container, the countdown, the start-up markers in the log, and the log's last lines (not for the read key) |
| `POST /api/v1/server/restart` | team | Restart after a countdown (`{"wait_minutes", "reason"}`) |
| `POST /api/v1/server/cancel-restart` | team | Cancel the countdown |
| `POST /api/v1/server/stop` | admin | Stop after a countdown |
| `POST /api/v1/server/start` | admin | Start the server if it is stopped |
| `POST /api/v1/server/deploy` | admin | Put a staged module (and the staged haks) live after a countdown (`{"module", "wait_minutes"}`) |
| `POST /api/v1/server/full-deploy` | admin | Put live whatever staged differs: module, haks, NWNX image (`{"wait_minutes", "skip"}`) |
| `POST /api/v1/server/nwnx/pull` | admin | Pull the NWNX image without restarting |
| `POST /api/v1/server/nwnx/update` | admin | Move the server to the newest NWNX image, with a countdown if anyone is on |
| `GET /api/v1/server/release/preview` | admin | Per repository: the current tag, the next ones, the notes GitHub would write |
| `POST /api/v1/server/release` | admin | Tag a release on GitHub (`{"repo", "bump": "minor"|"major", "tag_override"}`) |
| `GET /api/v1/server/repos` | admin | Per repository: last release, commits since, CI runs, the staging run, open pull requests |
| `GET /api/v1/meta/systems` | admin | Every route with its key group, which parts are configured, which keys are set |
| `GET /api/v1/game/tlk/{strref}` | read | The text of a string reference (dialog.tlk, or the module's custom talk table from 16777216) |
| `GET /api/v1/game/2da/{table}` | read | A 2DA's columns and row count, and where the game finds it (a hak, the module, override, ovr or base) |
| `GET /api/v1/game/2da/{table}/{row}?names=true` | read | One row, `{column: cell}`; with `names`, the talk table text of its name and description columns |
| `GET /api/v1/game/items/{resref}?identified=true` | read | An item blueprint as the game's examine panel words it: name, kind, weight, value, level required, properties |
| `GET /api/v1/game/vault/{folder}` | read | The character files in one player's server vault folder, newest first (this and the next two need `GAME_VAULT=on`) |
| `GET /api/v1/game/vault/{folder}/{file}` | read | A character sheet from the vault file: name, description, race, alignment, classes and levels, ability scores, skill ranks, feats, the gear worn with its examine panels, the portrait |
| `POST /api/v1/game/vault/{folder}/{file}/retire` | admin | Take a character out of the vault by renaming its file (`{"dry_run": true}` by default); refused while anyone is online |

Each key opens its own routes and those of the groups below it: full and admin open everything, team opens team and read,
read opens only read. A known key on a route above it gets 403, a wrong key 401.

The long operations (restart, stop, deploys, NWNX update) answer at once and run in the background; `GET /api/v1/server/progress` follows
them. Only one of each runs at a time (409 otherwise).

How a restart reaches players: with Redis configured, the API publishes a `restart` command that the module's NWNX_Redis pub/sub script
turns into an in-game countdown; the game shuts itself down at zero and the container's restart policy brings it back. The message format
is in [docs/contracts/restart.md](docs/contracts/restart.md). Without Redis, the API waits out the countdown itself and restarts the
container, and players get no warning in game.

How the server is started again after a stop or a deploy: a Coolify restart of the game's service (which makes the containers again, so a
new `NWN_MODULE` or NWNX image is used), or, without Coolify, the command in `GAME_START_COMMAND` run on the game host.

## Settings

Every setting is an environment variable. Only `OPS_API_KEY` is required: each part of the API switches itself on when its settings are
there, a route whose part is off answers 503 naming the settings it needs, and `GET /api/v1/meta/systems` lists what is on. The API
contacts nothing at start-up, so it starts (and can bring the game back) whatever else is down. [.env.example](.env.example) has them all
with comments.

| Setting | Default | What |
|---|---|---|
| `OPS_API_KEY` | (required, 16+ characters) | The full key: opens every route |
| `OPS_API_ADMIN_KEY` | | Opens every route too; for your own tools, so the full key can stay in a safe |
| `OPS_API_TEAM_KEY` | | The read routes, plus logs, restart, cancel-restart |
| `OPS_API_READ_KEY` | | Status, modules and progress (without its log lines), nothing else: for a public status page. |
| `GAME_HOST` | | The game host's private address. Switches on everything that looks at the game |
| `SSH_USER` | `nwn` | The user the API logs in as. It must be able to run `docker` |
| `SSH_KEY_PATH` | `/data/ssh/id_ed25519` | Its private key, in the container |
| `SSH_KNOWN_HOSTS` | | A known_hosts file with the game host's key. Unset, the key is not checked (a warning is logged) |
| `SSH_SUDO` | `false` | Run the commands that change the game's files (`mv`, `sed`, `rm`, the deploy hook) with `sudo -n`, for a game folder the SSH user can't write. It then needs passwordless sudo for those |
| `CONTAINER_FILTER` | `nwserver` | Containers whose name contains this are the game server |
| `GAME_DIR` | | The game's folder on the game host; fills in the paths below in the layout `home/modules`, `home/modules/.staged`, `home/hak`, `home/hak/.staged`, `home/tlk`, `home/development`, `logs/nwserverLog1.txt`, `.env` |
| `MOD_LIVE`, `MOD_STAGE`, `HAK_LIVE`, `HAK_STAGE`, `TLK_LIVE`, `DEVELOPMENT_DIR`, `LOG_FILE`, `NWN_ENV_FILE` | from `GAME_DIR` | Each path on its own, when your layout differs |
| `COOLIFY_ENV_FILE` | | Coolify's copy of the service's env file on the game host, rewritten at a deploy too |
| `MODULE_VERSION_REGEX` | `(?P<major>\d+)[-_.](?P<minor>\d+)$` | How a module name (`world_2-05`) becomes a version (`2.05`) in messages |
| `DEPLOY_HOOK` | | A command run on the game host after a deploy moved the files and before the start, with `MODULE` set |
| `GAME_START_COMMAND` | | Without Coolify: how to start the game, e.g. `cd ~/game && docker compose up -d` |
| `NWN_PORT` | `5121` | The game's UDP port, for counting players with the game's own status query |
| `COOLIFY_API_URL`, `COOLIFY_TOKEN`, `COOLIFY_SERVICE_UUID` | | The game as a Coolify service. Not `COOLIFY_URL`: Coolify sets that one in every container it runs |
| `REDIS_URL` | | The game's Redis (NWNX_Redis), for the in-game countdown |
| `REDIS_PREFIX` | `nwn:` | Commands go to `<prefix>cmd`; the module must use the same prefix |
| `REDIS_REPLY_TIMEOUT` | `5` | Seconds to wait for the game's reply |
| `PLAYER_COUNT_SQL`, `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_DB` | | Optional: count players (and DMs) with a query on the game database instead of the status query |
| `NWNX_IMAGE` | `ghcr.io/nwnxee/unified:latest` | The image an NWNX update pulls |
| `NWNX_REPO` | `nwnxee/unified` | The repository it is built from, for commit links |
| `GITHUB_TOKEN`, `REPOS` | | Releases and the repository overview. `REPOS` is `key=owner/name@branch[#workflow.yml]`, comma separated |
| `RELEASE_NAME` | `{tag}` | A release's title (`{tag}`, `{repo}`) |
| `RELEASE_TAG_REGEX`, `RELEASE_TAG_FORMAT` | `^v(\d+)\.(\d+)$`, `v{major}.{minor}` | Which tags are releases, and how the next is written |
| `RELEASE_BUMP_MESSAGE` | | A commit your release workflow adds on top of the tag (`{version}`), so the overview doesn't list it as waiting |
| `DISCORD_WEBHOOK_URL`, `DISCORD_USERNAME`, `DISCORD_AVATAR_URL`, `SERVER_NAME` | | Announcements of restarts and updates, sent only when someone is online (deploys always announce) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` and the other `OTEL_*` | | Request tracing with OpenTelemetry, off unless an endpoint is set |
| `GAME_NWN_ROOT` | | The game install, in the API's container (`data/*.key` and bifs, `lang/<language>/data/dialog.tlk`, `ovr/`). Switches on the game data routes |
| `GAME_LANGUAGE` | `en` | The language folder under `lang/` |
| `GAME_MODULE_FILE` | | A copy of the module: its item blueprints, and its hak list and custom talk table when the next settings are unset |
| `GAME_HAK_DIR`, `GAME_HAKS` | | The haks' folder, and their names in hak list order (comma separated; unset: from the module) |
| `GAME_TLK_DIR`, `GAME_CUSTOM_TLK` | `GAME_HAK_DIR`, from the module | The custom talk table's folder and name |
| `GAME_OVERRIDE_DIR` | | An override folder the server uses |
| `GAME_ITEM_LEVEL_RESTRICTION` | `true` | Whether the server's item level restriction is on: items then show "Level Required" |
| `GAME_VAULT` | `off` | `on` switches on the vault routes (with `GAME_HOST` and the vault's path; a character sheet needs `GAME_NWN_ROOT` too). Off by default: vault folder names are players' CD keys and the read key opens the listing |
| `SERVERVAULT` | `GAME_DIR/home/servervault` | The server vault on the game host, read over SSH |

## Running it

On the control host, as the deploy user:

```sh
git clone https://github.com/plenarius/nwn-ops-api.git && cd nwn-ops-api
cp .env.example .env && chmod 600 .env        # then fill it in
mkdir -p ssh && ssh-keygen -t ed25519 -N '' -f ssh/id_ed25519 -C nwn-ops-api
ssh-keyscan -t ed25519 GAME_PRIVATE_ADDRESS > ssh/known_hosts
# Let the key in on the game host (as the user that runs docker there):
ssh nwn@GAME_PRIVATE_ADDRESS 'cat >> ~/.ssh/authorized_keys' < ssh/id_ed25519.pub
sudo chown 1000 ssh/id_ed25519                # the container runs as uid 1000
docker compose up -d --build
curl -s http://CONTROL_PRIVATE_ADDRESS:8100/health
```

Or as a Docker Compose resource in Coolify on the control host, from this repository, with the settings in its environment variables and
the two SSH files mounted the same way.

A first look:

```sh
KEY=...   # read it from a file rather than typing it on the command line
curl -s -H "Authorization: Bearer $KEY" http://CONTROL_PRIVATE_ADDRESS:8100/api/v1/server/status
curl -s -H "Authorization: Bearer $KEY" -X POST -H 'Content-Type: application/json' \
     -d '{"wait_minutes": 5, "reason": "update"}' http://CONTROL_PRIVATE_ADDRESS:8100/api/v1/server/restart
```

Running the tests: `pip install -r requirements.txt && python -m pytest -q` (Python 3.12).

## Security

- **Bind it to the private address only.** The API can stop your game and change what it runs. `compose.yaml` publishes it on
  `OPS_API_BIND` and refuses to start without it; set it to the control host's WireGuard address, never `0.0.0.0` or a public address,
  and keep the port closed at your provider's firewall too. The private interface must be up before Docker starts the container.
- **One key per caller.** Give a public status page the read key (it can't restart the game or read its log), a bot or a GM page the team key, your own tools the admin key, and keep the full key out of daily use.
  Keys are compared in constant time. Make them with `openssl rand -hex 32`; a key shorter than 16 characters is refused at start-up.
  Changing a key is a restart of the API with the new value, then the same change in the callers.
- **The API trusts its callers.** `reason` and the like are whatever the caller sends; a leaked key is that caller's powers.
- **SSH.** The API's key logs in as a user that can run `docker` on the game host, which is root-equivalent there. Use a key made only for
  the API, and set `SSH_KNOWN_HOSTS` so the API refuses a host that is not yours.
- **Secrets are never logged.** Tokens, passwords and the webhook URL are kept out of the logs and out of `/api/v1/meta/systems`, which
  shows only whether each key is set. Coolify's answers echo setting values, and are never logged either.
- **Redis.** Reach the game's Redis over the private network only, and publish its port on the private address alone: anyone who can
  publish on the command channel can restart the game. A password is no help with upstream NWNX_Redis, which has no setting for one: its
  pub/sub connection can't log in to a Redis that asks, and then quietly subscribes to nothing.
- **A restart of the API in the middle of a countdown or deploy loses it.** The operations run inside the API's process, with no record to
  resume from. Don't redeploy the API while one runs (`GET /api/v1/server/progress` shows them).

## Game data

The `/api/v1/game` routes read the game's own files, so a website can show a character sheet, an item or a name the way the game words it
without a game client or a toolset. They are read-only (except retire, admin key) and expose nothing private, so the read key opens them.

- **The files are the API's own copy.** Mount a folder holding the game install, the module, its haks and custom talk table read-only into
  the container (`compose.yaml` has the line to uncomment), and keep it in step with the server: after a hak or module deploy, copy the new
  files there too. The API sees changed files within ten seconds, without a restart. Character files are read from the game host over SSH
  when asked, so a sheet is always the file as the game last saved it.
- **Files are found the way the game finds them:** the haks in hak list order (the first listed wins), then the module, then the
  override folder, then the install's `ovr` folders, then the base game's key and bif files (`nwn_retail` before `nwn_base`, the language
  folder's first). Each answer says where its file came from.
- **What the numbers are.** An item's value, weight, required level and property wording are ports of the server's and client's own code
  (`CNWSItem::GetCost`, `GetMinEquipLevel`, `ComputeWeight`, `CGuiExamineItem::AddProperty`); its value and level matched the engine on
  every one of about 9,000 blueprints of the base game and of a large persistent world. Ability scores are the base score, the race's and
  classes' adjustments and the worn items' bonuses and penalties, by the server's caps. Spells and effects applied in the running game are not
  in the file and not counted, and skills show the ranks the character bought, not the totals the game's sheet adds up.
- **Unidentified items** show only what a player sees before identifying them: the base item's name and description, and the weight.
- **Names keep the game's colour codes** (`<cRGB>…</c>`), as a player-given name holds them.
- **Retire** renames `<file>.bic` to `<file>.bic.retired-<UTC time>` (and the game's `.bak` beside it) in the same folder: the character
  leaves the player's list and nothing is deleted; renaming it back restores it. It runs only when the server is stopped or reports nobody
  online, because a character who is logged in would be saved again under its old name.

## Known limits

- `POST /restart` answers before the game has replied. If Redis is configured but the game does not answer and the container is running
  (a hung module), nothing more happens; the API's log says so.
- Without Redis, a restart's countdown is not shown by `GET /status` or `GET /progress` (only that a `restart` operation runs);
  `POST /cancel-restart` still stops it.
- A module deploy moves every staged `.mod` file to the live folder, not only the one deployed (only the chosen one becomes `NWN_MODULE`).
- A full deploy stops the container right after the countdown; a module deploy waits 15 seconds more for the game's own save.
- Without a database query, the status query counts players only (not DMs), and an unanswered query counts as nobody online.
- An NWNX update pulls `NWNX_IMAGE` and makes the container again; it doesn't change the game's own image setting. To update through the
  API, the game runs on the same tag as `NWNX_IMAGE` (a moving one, such as a build tag). A game on another tag (a pinned commit) is left
  alone, with a message saying so, and `GET /status` keeps showing the update as available.

## Licence

MIT. See [LICENSE](LICENSE).
