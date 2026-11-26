"""Settings: every field is an environment variable (the upper-cased field name, no prefix). .env.example lists them all.

Only OPS_API_KEY is required. Each part of the API switches itself on when its settings are there (features()), and a route whose part
is off answers 503 naming the settings it is missing. Nothing is contacted at start-up.
"""

import re
from dataclasses import dataclass
from typing import ClassVar

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings


@dataclass(frozen=True)
class Repo:
    """One GitHub repository the API releases and reports on: REPOS entry `key=owner/name@branch` or `key=owner/name@branch#workflow.yml`."""
    key: str
    full_name: str
    branch: str
    staging_workflow: str = ""


_REPO = re.compile(r"^([\w.-]+)=([\w.-]+/[\w.-]+)@([^#\s]+)(?:#([\w.-]+))?$")


def parse_repos(spec: str) -> dict[str, Repo]:
    out: dict[str, Repo] = {}
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        m = _REPO.match(part)
        if not m:
            raise ValueError(f"REPOS entry {part!r} is not key=owner/name@branch[#workflow.yml]")
        out[m.group(1)] = Repo(m.group(1), m.group(2), m.group(3), m.group(4) or "")
    return out


class Settings(BaseSettings):
    app_title: str = "NWN Ops API"
    # What the notices call the server ("The server restarts in 5 minutes").
    server_name: str = "The server"

    # --- keys (app/auth.py). The full key opens everything; the others only their group. -----------------------------------------------
    ops_api_key: str = Field(min_length=16, repr=False)
    ops_api_admin_key: str = Field("", repr=False)
    ops_api_team_key: str = Field("", repr=False)
    # Read-only: status, modules and progress (without its log lines). For a public status page, which must not be able to restart.
    ops_api_read_key: str = Field("", repr=False)

    # --- the game host, over SSH ----------------------------------------------------------------------------------------------------------
    # Its private address (WireGuard). Unset: every route that looks at the game answers 503.
    game_host: str = ""
    ssh_user: str = "nwn"
    ssh_key_path: str = "/data/ssh/id_ed25519"
    # A known_hosts file holding the game host's key. Unset, the host key is not checked (logged as a warning at start-up).
    ssh_known_hosts: str = ""
    # Run the commands that change the game's files (mv, sed, rm, the deploy hook) with `sudo -n`, for a game folder the SSH user can't
    # write (the guide's layout, made with sudo). The user then needs passwordless sudo for those commands.
    ssh_sudo: bool = False
    # Containers whose name contains this are the game server (docker ps --filter name=...).
    container_filter: str = "nwserver"

    # Where the game's files are on the game host. GAME_DIR fills in any of the paths below left unset, in the guide's layout
    # (GAME_DIR/home/modules, GAME_DIR/home/modules/.staged, ..., GAME_DIR/logs/nwserverLog1.txt, GAME_DIR/.env).
    game_dir: str = ""
    mod_live: str = ""
    mod_stage: str = ""
    hak_live: str = ""
    hak_stage: str = ""
    # The custom talk table folder: a staged *.tlk goes here, not to the hak folder.
    tlk_live: str = ""
    # Hot-loaded overrides, emptied after a successful module deploy. Unset: left alone.
    development_dir: str = ""
    log_file: str = ""
    # The plain env file holding NWN_MODULE (the written copy in tier 2). A deploy rewrites its NWN_MODULE line.
    nwn_env_file: str = ""
    # Coolify's own copy of the service's env file on the game host, also rewritten at a deploy. Optional (the API patch is what counts).
    coolify_env_file: str = ""
    # Turns a module name into its version for messages: the named groups major and minor, else the whole name is used.
    module_version_regex: str = r"(?P<major>\d+)[-_.](?P<minor>\d+)$"
    # A command run on the game host after a deploy moved the files and before the server starts (MODULE=<name> is set for it).
    # A failure is reported and the server is still started. Unset: none.
    deploy_hook: str = ""
    # How to start the game when Coolify is not configured (tier 1), run over SSH, e.g. `cd ~/game && docker compose up -d`.
    game_start_command: str = ""

    # The game's UDP port, for the status query that counts players (the same question a player's game asks).
    nwn_port: int = 5121

    # --- Coolify --------------------------------------------------------------------------------------------------------------------------
    # Not COOLIFY_URL: Coolify sets its own COOLIFY_URL in every container it runs, which would override this one.
    coolify_url: str = Field("", validation_alias="COOLIFY_API_URL")
    coolify_token: str = Field("", repr=False)
    coolify_service_uuid: str = ""

    # --- Redis (NWNX_Redis pub/sub, docs/contracts/restart.md) ----------------------------------------------------------------------------
    # Unset: no in-game countdown; a restart waits the countdown and restarts the container.
    redis_url: str = Field("", repr=False)
    # Shared with the module: commands go to <prefix>cmd, replies come back in <prefix>response:<id>.
    redis_prefix: str = "nwn:"
    redis_reply_timeout: float = 5.0

    # --- player counts --------------------------------------------------------------------------------------------------------------------
    # Optional: a query on the game database returning (players[, dms]) for the people online, used instead of the UDP status query.
    player_count_sql: str = ""
    # Optional: a query on the game database returning one row per character to show on a roster of who's online, the character's name
    # first; any further columns come back under their own names. Leave DMs out in the query itself, so they are not even counted.
    online_sql: str = ""
    mysql_host: str = ""
    mysql_port: int = 3306
    mysql_user: str = "nwn"
    mysql_password: str = Field("", repr=False)
    mysql_db: str = "nwn"
    mysql_pool_size: int = 4

    # --- NWNX -----------------------------------------------------------------------------------------------------------------------------
    nwnx_image: str = "ghcr.io/nwnxee/unified:latest"
    # The repository the image is built from, for commit links and "commits behind".
    nwnx_repo: str = "nwnxee/unified"

    # --- GitHub releases and the repository overview --------------------------------------------------------------------------------------
    github_token: str = Field("", repr=False)
    # key=owner/name@branch[#workflow.yml], comma separated. The key is what the release route takes; the workflow is the one whose newest
    # run stages what a deploy puts live.
    repos: str = ""
    release_name: str = "{tag}"
    # The tags that count as releases (groups 1 and 2: major and minor) and how the next one is written.
    release_tag_regex: str = r"^v(\d+)\.(\d+)$"
    release_tag_format: str = "v{major}.{minor}"
    # A commit the release workflow adds on top of the tag (its first line, with {version} for the tag without its "v"); it belongs to the
    # release, so the overview does not list it as waiting. Unset: none.
    release_bump_message: str = ""

    # --- the dev server (app/services/dev.py) --------------------------------------------------------------------------------------------
    # The module repository whose DEV_WORKFLOW loads a build onto the dev server (GITHUB_TOKEN needs Actions: write on it), the branch the
    # workflow runs from, and the dev server's status.json, mounted read-only (unset: status shows only the last workflow run).
    dev_repo: str = ""
    dev_workflow: str = "dev-load.yml"
    dev_ref: str = "main"
    dev_status_file: str = ""

    # --- game data (app/gamedata): read-only routes a website uses. Off unless GAME_NWN_ROOT is set (the vault: GAME_VAULT). --------------
    # Folders the API itself reads (mounted read-only into its container), not paths on the game host.
    # The game's install: data/*.key and the bifs, lang/<language>/data/dialog.tlk, ovr/.
    game_nwn_root: str = ""
    game_language: str = "en"
    # The haks, and their names in the module's hak list order (the first wins), comma separated. GAME_HAKS unset: read from the module.
    game_hak_dir: str = ""
    game_haks: str = ""
    # The custom talk table's folder (unset: GAME_HAK_DIR) and name without .tlk (unset: read from the module).
    game_tlk_dir: str = ""
    game_custom_tlk: str = ""
    # A copy of the module (.mod): its blueprints, and its hak list and custom talk table when the two settings above are unset.
    game_module_file: str = ""
    # An override folder the server uses, if any.
    game_override_dir: str = ""
    # The server's item level restriction (nwnplayer.ini ILR): an item's examine panel shows its "Level Required" only when on.
    game_item_level_restriction: bool = True
    # The vault routes list and read players' character files, and vault folder names are CD keys, so they stay off until switched on
    # here, whatever paths are set.
    game_vault: bool = False
    # The server vault on the game host (read over SSH). Unset: GAME_DIR/home/servervault.
    servervault: str = ""

    # --- Discord --------------------------------------------------------------------------------------------------------------------------
    discord_webhook_url: str = Field("", repr=False)
    discord_username: str = "Server"
    discord_avatar_url: str = ""

    model_config = {"env_prefix": "", "populate_by_name": True}

    @model_validator(mode="after")
    def _paths(self):
        if self.game_dir:
            g = self.game_dir.rstrip("/")
            defaults = {
                "mod_live": f"{g}/home/modules", "mod_stage": f"{g}/home/modules/.staged",
                "hak_live": f"{g}/home/hak", "hak_stage": f"{g}/home/hak/.staged", "tlk_live": f"{g}/home/tlk",
                "development_dir": f"{g}/home/development", "log_file": f"{g}/logs/nwserverLog1.txt", "nwn_env_file": f"{g}/.env",
                "servervault": f"{g}/home/servervault",
            }
            for k, v in defaults.items():
                if not getattr(self, k):
                    setattr(self, k, v)
        for name in ("ops_api_admin_key", "ops_api_team_key", "ops_api_read_key"):
            if 0 < len(getattr(self, name)) < 16:
                raise ValueError(f"{name.upper()} must be at least 16 characters")
        if self.ops_api_read_key:
            if self.ops_api_read_key in (self.ops_api_key, self.ops_api_admin_key, self.ops_api_team_key):
                # A shared value would make the read key the other key too (or hide the progress log from that caller).
                raise ValueError("OPS_API_READ_KEY must differ from the other keys")
        if self.dev_repo and not re.match(r"^[\w.-]+/[\w.-]+$", self.dev_repo):
            raise ValueError("DEV_REPO is owner/name")
        parse_repos(self.repos)  # refuse a bad REPOS at start-up
        re.compile(self.module_version_regex)
        re.compile(self.release_tag_regex)
        return self

    @property
    def sudo(self) -> str:
        return "sudo -n " if self.ssh_sudo else ""

    @property
    def repo_map(self) -> dict[str, Repo]:
        return parse_repos(self.repos)

    def module_version(self, name: str) -> str:
        m = re.search(self.module_version_regex, name or "")
        if m and "major" in m.groupdict() and "minor" in m.groupdict():
            return f"{int(m.group('major'))}.{m.group('minor')}"
        return name

    def module_label(self, name: str) -> str:
        """How messages name a module: "v2.05" when MODULE_VERSION_REGEX finds a version in it, else the name itself."""
        version = self.module_version(name)
        return f"v{version}" if version != name else name

    def features(self) -> dict[str, bool]:
        return {
            "game": bool(self.game_host),
            "deploy": bool(self.game_host and self.mod_live and self.mod_stage),
            "haks": bool(self.game_host and self.hak_live and self.hak_stage),
            "logs": bool(self.game_host and self.log_file),
            "coolify": bool(self.coolify_url and self.coolify_token and self.coolify_service_uuid),
            "start_command": bool(self.game_host and self.game_start_command),
            "redis": bool(self.redis_url),
            "player_db": bool(self.mysql_host and self.player_count_sql),
            "roster": bool(self.mysql_host and self.online_sql),
            "github": bool(self.github_token and self.repos),
            "discord": bool(self.discord_webhook_url),
            "game_data": bool(self.game_nwn_root),
            "dev": bool(self.github_token and self.dev_repo),
            "vault": bool(self.game_vault and self.game_host and self.servervault),
        }

    # Which settings switch each part on, for the 503 answers and /api/v1/meta/systems (names only, never values).
    NEEDS: ClassVar[dict[str, str]] = {
        "game": "GAME_HOST",
        "deploy": "GAME_HOST and GAME_DIR (or MOD_LIVE and MOD_STAGE)",
        "haks": "GAME_HOST and GAME_DIR (or HAK_LIVE and HAK_STAGE)",
        "logs": "GAME_HOST and GAME_DIR (or LOG_FILE)",
        "coolify": "COOLIFY_API_URL, COOLIFY_TOKEN and COOLIFY_SERVICE_UUID",
        "start_command": "GAME_HOST and GAME_START_COMMAND",
        "redis": "REDIS_URL",
        "player_db": "MYSQL_HOST and PLAYER_COUNT_SQL",
        "roster": "MYSQL_HOST and ONLINE_SQL",
        "github": "GITHUB_TOKEN and REPOS",
        "discord": "DISCORD_WEBHOOK_URL",
        "game_data": "GAME_NWN_ROOT",
        "dev": "GITHUB_TOKEN and DEV_REPO",
        "vault": "GAME_VAULT=on, GAME_HOST and GAME_DIR (or SERVERVAULT)",
    }
