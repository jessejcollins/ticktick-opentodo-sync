"""Configuration from environment variables (optionally pre-loaded from a .env file)."""

from __future__ import annotations

import difflib
import ipaddress
import logging
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .ticktick import BASE_URL as TICKTICK_BASE_URL


class ConfigError(Exception):
    pass


PREFIX = "TICKTICK_OPENTODO_SYNC_"
OLD_PREFIX = "TICKSYNC_"
KNOWN_SETTINGS = frozenset(
    PREFIX + name
    for name in (
        "DRY_RUN",
        "MAX_DESTRUCTIVE",
        "MAX_DELETES_PER_DAY",
        "INTERVAL",
        "TIMEZONE",
        "LOG_LEVEL",
        "HTTP_MIN_INTERVAL",
        "HTTP_TIMEOUT",
        "STATE_DIR",
        "RUN_DIR",
        "ALLOW_HTTP",
    )
)


def unknown_settings(names: Iterable[str]) -> list[str]:
    """Warnings for variables that look like ours but aren't read (typos, the old prefix)."""
    out = []
    for name in sorted(names):
        if name.startswith(OLD_PREFIX):
            new = PREFIX + name.removeprefix(OLD_PREFIX)
            hint = f"rename it to {new}" if new in KNOWN_SETTINGS else "the old prefix is not read"
            out.append(f"ignoring {name}: {hint}")
        elif name.startswith(PREFIX) and name not in KNOWN_SETTINGS:
            # Compare suffixes only: the long shared prefix would make everything look close.
            suffixes = [k.removeprefix(PREFIX) for k in KNOWN_SETTINGS]
            close = difflib.get_close_matches(name.removeprefix(PREFIX), suffixes, n=1, cutoff=0.75)
            hint = f"did you mean {PREFIX}{close[0]}?" if close else "unknown setting"
            out.append(f"ignoring {name}: {hint}")
    return out


@dataclass(frozen=True)
class Config:
    ticktick_token: str = field(repr=False)
    opentodo_url: str
    opentodo_token: str = field(repr=False)
    ticktick_base_url: str = TICKTICK_BASE_URL
    state_dir: Path = Path("state")
    run_dir: Path | None = None
    dry_run: bool = True
    max_destructive: int = 10
    max_deletes_per_day: int = 50
    timezone: str | None = None
    interval: int = 300
    http_min_interval: float = 0.3
    http_timeout: float = 20.0
    log_level: str = "INFO"
    opentodo_passphrase: str | None = field(default=None, repr=False)
    opentodo_recovery_key: str | None = field(default=None, repr=False)
    """OpenTodo end-to-end encryption secrets; one is needed when encryption is on."""
    warnings: tuple[str, ...] = field(default=(), compare=False)
    """Non-fatal problems found while loading; logged at startup."""

    @property
    def db_path(self) -> Path:
        return self.state_dir / "ticktick-opentodo-sync.sqlite3"

    @property
    def lock_path(self) -> Path:
        return self.state_dir / "ticktick-opentodo-sync.lock"

    @property
    def heartbeat_path(self) -> Path | None:
        return self.run_dir / "heartbeat.json" if self.run_dir else None


def read_env_file(path: Path) -> dict[str, str]:
    """Minimal .env parser: KEY=VALUE, optional ``export``, quotes, ``#`` comments."""
    out: dict[str, str] = {}
    for lineno, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").lstrip()
        key, sep, value = line.partition("=")
        if not sep or not key.strip().isidentifier():
            raise ConfigError(f"{path}:{lineno}: expected KEY=VALUE")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        out[key.strip()] = value
    return out


def _bool(name: str, value: str) -> bool:
    v = value.strip().lower()
    if v in {"1", "true", "yes", "on"}:
        return True
    if v in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name}: expected true/false, got {value!r}")


def _int(name: str, value: str, minimum: int) -> int:
    try:
        n = int(value)
    except ValueError:
        raise ConfigError(f"{name}: expected an integer, got {value!r}") from None
    if n < minimum:
        raise ConfigError(f"{name}: must be >= {minimum}")
    return n


def _float(name: str, value: str, minimum: float) -> float:
    try:
        n = float(value)
    except ValueError:
        raise ConfigError(f"{name}: expected a number, got {value!r}") from None
    if n < minimum:
        raise ConfigError(f"{name}: must be >= {minimum}")
    return n


def load(env: Mapping[str, str] | None = None, env_file: Path | None = None) -> Config:
    """Process environment wins over ``env_file``."""
    merged: dict[str, str] = {}
    if env_file is not None and env_file.is_file():
        merged |= read_env_file(env_file)
    merged |= dict(os.environ if env is None else env)

    def get(name: str, default: str | None = None) -> str | None:
        value = merged.get(name, "").strip()
        return value or default

    missing = [n for n in ("TICKTICK_TOKEN", "OPENTODO_URL", "OPENTODO_TOKEN") if not get(n)]
    if missing:
        raise ConfigError(f"missing required setting(s): {', '.join(missing)}")

    tz = get("TICKTICK_OPENTODO_SYNC_TIMEZONE")
    if tz:
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            raise ConfigError(
                f"TICKTICK_OPENTODO_SYNC_TIMEZONE: unknown time zone {tz!r}"
            ) from None

    warnings = unknown_settings(merged)
    allow_http = _bool(PREFIX + "ALLOW_HTTP", get(PREFIX + "ALLOW_HTTP", "false") or "false")
    opentodo_url = _base_url("OPENTODO_URL", str(get("OPENTODO_URL")), allow_http, warnings)
    ticktick_url = _base_url(
        "TICKTICK_BASE_URL", str(get("TICKTICK_BASE_URL", TICKTICK_BASE_URL)), allow_http, warnings
    )

    run_dir = get("TICKTICK_OPENTODO_SYNC_RUN_DIR")
    return Config(
        ticktick_token=str(get("TICKTICK_TOKEN")),
        opentodo_url=opentodo_url,
        opentodo_token=str(get("OPENTODO_TOKEN")),
        ticktick_base_url=ticktick_url,
        state_dir=Path(
            str(get("TICKTICK_OPENTODO_SYNC_STATE_DIR", get("STATE_DIRECTORY", "state")))
        ),
        run_dir=Path(run_dir) if run_dir else None,
        dry_run=_bool(
            "TICKTICK_OPENTODO_SYNC_DRY_RUN",
            get("TICKTICK_OPENTODO_SYNC_DRY_RUN", "true") or "true",
        ),
        max_destructive=_int(
            "TICKTICK_OPENTODO_SYNC_MAX_DESTRUCTIVE",
            str(get("TICKTICK_OPENTODO_SYNC_MAX_DESTRUCTIVE", "10")),
            0,
        ),
        max_deletes_per_day=_int(
            "TICKTICK_OPENTODO_SYNC_MAX_DELETES_PER_DAY",
            str(get("TICKTICK_OPENTODO_SYNC_MAX_DELETES_PER_DAY", "50")),
            0,
        ),
        timezone=tz,
        interval=_int(
            "TICKTICK_OPENTODO_SYNC_INTERVAL",
            str(get("TICKTICK_OPENTODO_SYNC_INTERVAL", "300")),
            60,
        ),
        http_min_interval=_float(
            "TICKTICK_OPENTODO_SYNC_HTTP_MIN_INTERVAL",
            str(get("TICKTICK_OPENTODO_SYNC_HTTP_MIN_INTERVAL", "0.3")),
            0.0,
        ),
        http_timeout=_float(
            "TICKTICK_OPENTODO_SYNC_HTTP_TIMEOUT",
            str(get("TICKTICK_OPENTODO_SYNC_HTTP_TIMEOUT", "20")),
            1.0,
        ),
        log_level=_log_level(str(get("TICKTICK_OPENTODO_SYNC_LOG_LEVEL", "INFO"))),
        # Not stripped: whitespace may be part of a passphrase.
        opentodo_passphrase=merged.get("OPENTODO_ENCRYPTION_PASSPHRASE") or None,
        opentodo_recovery_key=get("OPENTODO_ENCRYPTION_RECOVERY_KEY"),
        warnings=tuple(warnings),
    )


def _log_level(value: str) -> str:
    """Checked here: logging.basicConfig raised on an unknown one, a traceback at startup."""
    level = value.strip().upper()
    if level not in logging.getLevelNamesMapping():
        raise ConfigError(
            f"TICKTICK_OPENTODO_SYNC_LOG_LEVEL: expected DEBUG, INFO, WARNING or ERROR, "
            f"got {value!r}"
        )
    return level


def _base_url(name: str, url: str, allow_http: bool, warnings: list[str]) -> str:
    """A service's base URL: https://, or http:// to this machine or when explicitly allowed.

    Over plain http the bearer token travels unencrypted, and for OpenTodo so does the
    encryption key record (salt, iterations, wrapped key: enough to guess the passphrase
    offline). The URL is never echoed: it could carry credentials.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ConfigError(f"{name} must be a URL like https://todo.example.com")
    if parts.scheme == "http" and not _loopback(parts.hostname):
        if not allow_http:
            raise ConfigError(
                f"{name} uses http://, which sends the token (and OpenTodo's encryption key "
                f"record) unencrypted. Use https://, or set {PREFIX}ALLOW_HTTP=true for a "
                "server on a network you trust"
            )
        warnings.append(f"{name} uses http://: the token travels unencrypted")
    return url.rstrip("/")


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
