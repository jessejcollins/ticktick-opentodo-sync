from __future__ import annotations

from pathlib import Path

import pytest

from ticktick_opentodo_sync.config import ConfigError, load, read_env_file, unknown_settings

REQUIRED = {"TICKTICK_TOKEN": "tt", "OPENTODO_URL": "https://ot.example/", "OPENTODO_TOKEN": "ot"}


def test_defaults() -> None:
    cfg = load(REQUIRED)
    assert cfg.dry_run is True
    assert cfg.max_destructive == 10
    assert cfg.max_deletes_per_day == 50
    assert cfg.interval == 300
    assert cfg.opentodo_url == "https://ot.example"
    assert cfg.timezone is None
    assert cfg.log_level == "INFO"
    assert load(REQUIRED | {"TICKTICK_OPENTODO_SYNC_LOG_LEVEL": " debug "}).log_level == "DEBUG"


def test_missing_required() -> None:
    with pytest.raises(ConfigError, match="OPENTODO_TOKEN"):
        load({"TICKTICK_TOKEN": "x", "OPENTODO_URL": "https://x"})


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("TICKTICK_OPENTODO_SYNC_DRY_RUN", "maybe"),
        ("TICKTICK_OPENTODO_SYNC_TIMEZONE", "Mars/Base"),
        ("TICKTICK_OPENTODO_SYNC_INTERVAL", "10"),
        ("TICKTICK_OPENTODO_SYNC_MAX_DESTRUCTIVE", "x"),
        ("TICKTICK_OPENTODO_SYNC_MAX_DELETES_PER_DAY", "-1"),
        ("TICKTICK_OPENTODO_SYNC_ALLOW_HTTP", "maybe"),
        ("TICKTICK_OPENTODO_SYNC_LOG_LEVEL", "VERBOSE"),  # basicConfig raised: a traceback
        ("OPENTODO_URL", "ot.example"),
        ("OPENTODO_URL", "ftp://ot.example"),
        ("OPENTODO_URL", "https://"),
    ],
)
def test_invalid_values(key: str, value: str) -> None:
    with pytest.raises(ConfigError):
        load(REQUIRED | {key: value})


@pytest.mark.parametrize(
    "url", ["http://todo.lan", "http://192.168.1.10:8080", "http://user:s3cret@todo.lan"]
)
def test_http_is_refused(url: str) -> None:
    """The full-scope token, and OpenTodo's encryption key record, would travel unencrypted."""
    with pytest.raises(ConfigError, match="ALLOW_HTTP=true") as err:
        load(REQUIRED | {"OPENTODO_URL": url})
    assert "todo.lan" not in str(err.value) and "s3cret" not in str(err.value)  # never echoed


@pytest.mark.parametrize("url", ["http://localhost:8080", "http://127.0.0.1", "http://[::1]:3000"])
def test_http_to_this_machine_is_fine(url: str) -> None:
    cfg = load(REQUIRED | {"OPENTODO_URL": url})
    assert cfg.opentodo_url == url and cfg.warnings == ()


def test_http_elsewhere_with_the_opt_in() -> None:
    cfg = load(
        REQUIRED
        | {"OPENTODO_URL": "http://opentodo:8080/", "TICKTICK_OPENTODO_SYNC_ALLOW_HTTP": "true"}
    )
    assert cfg.opentodo_url == "http://opentodo:8080"
    assert cfg.warnings == ("OPENTODO_URL uses http://: the token travels unencrypted",)


def test_ticktick_base_url_is_checked_too() -> None:
    with pytest.raises(ConfigError, match="TICKTICK_BASE_URL"):
        load(REQUIRED | {"TICKTICK_BASE_URL": "http://api.ticktick.com"})


def test_env_file_parsing_and_precedence(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    f.write_text(
        "# comment\n"
        "export TICKTICK_TOKEN='from file'\n"
        'OPENTODO_URL="https://file.example"\n'
        "OPENTODO_TOKEN=abc # trailing comment\n"
        "TICKTICK_OPENTODO_SYNC_DRY_RUN=false\n"
        "\n"
    )
    assert read_env_file(f)["OPENTODO_TOKEN"] == "abc"
    cfg = load({"TICKTICK_TOKEN": "from env"}, env_file=f)
    assert cfg.ticktick_token == "from env"
    assert cfg.opentodo_url == "https://file.example"
    assert cfg.dry_run is False


def test_env_file_bad_line(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    f.write_text("NOT A PAIR\n")
    with pytest.raises(ConfigError, match=":1:"):
        read_env_file(f)


def test_unknown_settings_warnings() -> None:
    cfg = load(
        REQUIRED
        | {
            "TICKSYNC_DRY_RUN": "false",
            "TICKSYNC_BOGUS": "1",
            "TICKTICK_OPENTODO_SYNC_DRYRUN": "false",
            "TICKTICK_OPENTODO_SYNC_WHATEVER": "x",
            "TICKTICK_OPENTODO_SYNC_INTERVAL": "300",
            "UNRELATED": "x",
        }
    )
    assert cfg.dry_run is True  # misspelled/old keys really are ignored
    assert cfg.warnings == (
        "ignoring TICKSYNC_BOGUS: the old prefix is not read",
        "ignoring TICKSYNC_DRY_RUN: rename it to TICKTICK_OPENTODO_SYNC_DRY_RUN",
        "ignoring TICKTICK_OPENTODO_SYNC_DRYRUN: did you mean TICKTICK_OPENTODO_SYNC_DRY_RUN?",
        "ignoring TICKTICK_OPENTODO_SYNC_WHATEVER: unknown setting",
    )
    assert unknown_settings(["TICKTICK_OPENTODO_SYNC_TIMEZONE", "OPENTODO_URL"]) == []
