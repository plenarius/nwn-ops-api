import pytest

from app.config import Repo, parse_repos
from tests.conftest import make_settings


def test_game_dir_fills_in_the_guides_layout():
    s = make_settings(game_dir="/home/nwn/game/", log_file="/var/log/nwn.txt")
    assert s.mod_live == "/home/nwn/game/home/modules" and s.mod_stage == "/home/nwn/game/home/modules/.staged"
    assert s.hak_live == "/home/nwn/game/home/hak" and s.tlk_live == "/home/nwn/game/home/tlk"
    assert s.nwn_env_file == "/home/nwn/game/.env"
    assert s.log_file == "/var/log/nwn.txt", "a path that is set is kept"


def test_no_paths_without_game_dir():
    s = make_settings(game_dir="")
    assert s.mod_live == "" and s.log_file == "" and not s.features()["deploy"]


def test_repos():
    assert parse_repos("module=me/mod@main#release.yml, haks=me/haks@64bit") == {
        "module": Repo("module", "me/mod", "main", "release.yml"), "haks": Repo("haks", "me/haks", "64bit", "")}
    assert parse_repos("") == {}
    with pytest.raises(ValueError):
        parse_repos("module=me/mod")


@pytest.mark.parametrize("name,regex,want", [
    ("MyWorld_6-17", None, "6.17"),
    ("world_1.05", None, "1.05"),
    ("mymodule", None, "mymodule"),
    ("build-42", r"build-(?P<major>\d+)(?P<minor>)", "42."),
])
def test_module_version(name, regex, want):
    s = make_settings(**({"module_version_regex": regex} if regex else {}))
    assert s.module_version(name) == want


def test_secrets_are_not_in_the_repr():
    s = make_settings(coolify_token="tok-secret", redis_url="redis://:pw-secret@h", discord_webhook_url="https://d/x/hook-secret")
    assert "secret" not in repr(s) and "test-full-key" not in repr(s)


def test_features_follow_the_settings():
    s = make_settings(coolify_url="https://c", coolify_token="t")
    assert s.features()["game"] and s.features()["deploy"] and not s.features()["coolify"]
    assert make_settings(coolify_url="https://c", coolify_token="t", coolify_service_uuid="u").features()["coolify"]
