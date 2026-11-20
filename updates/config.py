"""config.toml: which apps the run updates or reports, and how. Anything it does not understand stops the run."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from releases import normalize_image
from runtime import HERE

PATH = HERE / "config.toml"
POLICIES = ("floating", "semver", "report")
# Keys the updater itself uses in the mail and in state/.
RESERVED = {"docker", "images", "updater"}
KEY = re.compile(r"[A-Za-z0-9_-]+")
RELEASES = re.compile(r"github:[\w.-]+/[\w.-]+|hub:[\w.-]+/[\w.-]+|plex|qts")
# Releases of the NAS itself, read on the host by update.sh: no containers.
HOST = {"plex": "plex is the QNAP package", "qts": "qts is the NAS firmware"}
FIELDS = {"title", "policy", "containers", "members", "releases", "tag_prefix", "minor_wait_days", "image", "version_file", "version_command"}
MINOR_WAIT_DAYS = 7


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class App:
    key: str
    title: str
    policy: str
    containers: tuple[str, ...]
    members: tuple[str, ...] = ()
    releases: str = ""
    tag_prefix: str = ""
    image: str = ""
    version_file: str = ""
    version_command: tuple[str, ...] = ()
    minor_wait_days: int = MINOR_WAIT_DAYS

    @property
    def main(self) -> str:
        return self.containers[0]


@dataclass(frozen=True)
class Config:
    compose_container: str
    apps: dict[str, App]

    @property
    def titles(self) -> dict[str, str]:
        return {key: app.title for key, app in self.apps.items()}


def load(path: Path = PATH) -> Config:
    try:
        raw = tomllib.loads(path.read_text())
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path.name}: {exc}") from exc
    unknown = set(raw) - {"compose_container", "apps"}
    if unknown:
        raise ConfigError(f"{path.name}: unknown setting {sorted(unknown)[0]}")
    runner = raw.get("compose_container")
    if not isinstance(runner, str) or not runner:
        raise ConfigError(f"{path.name}: compose_container must name the container that runs Compose")
    tables = raw.get("apps")
    if not isinstance(tables, dict) or not tables:
        raise ConfigError(f"{path.name}: no [apps.<key>] tables")
    apps = {key: _app(path.name, key, table) for key, table in tables.items()}
    for source in HOST:
        same = [app.key for app in apps.values() if app.releases == source]
        if len(same) > 1:
            raise ConfigError(f"{path.name}: apps.{same[0]} and apps.{same[1]} both follow {source}")
    seen: dict[str, str] = {}
    for app in apps.values():
        for name in (*app.containers, *app.members):
            if name in seen:
                raise ConfigError(f"{path.name}: container {name} is in apps.{seen[name]} and apps.{app.key}")
            seen[name] = app.key
        if runner in app.containers and (app.policy != "floating" or len(app.containers) != 1):
            raise ConfigError(f"{path.name}: apps.{app.key}: {runner} runs Compose, so its app is floating and has only that container")
    return Config(runner, apps)


def _app(file: str, key: str, table) -> App:
    where = f"{file}: apps.{key}"
    if not KEY.fullmatch(key) or key in RESERVED:
        raise ConfigError(f"{where}: the key must be letters, digits, - or _, and not {', '.join(sorted(RESERVED))}")
    if not isinstance(table, dict):
        raise ConfigError(f"{where} must be a table")
    unknown = set(table) - FIELDS
    if unknown:
        raise ConfigError(f"{where}: unknown field {sorted(unknown)[0]}")
    title = _text(where, table, "title", required=True)
    policy = _text(where, table, "policy", required=True)
    if policy not in POLICIES:
        raise ConfigError(f"{where}.policy must be one of {', '.join(POLICIES)}")
    containers = _names(where, table, "containers")
    members = _names(where, table, "members")
    releases = _text(where, table, "releases")
    if releases and not RELEASES.fullmatch(releases):
        raise ConfigError(f"{where}.releases must be github:<owner>/<repo>, hub:<namespace>/<repo> (hub:library/<name> for official images), plex or qts")
    if policy in ("semver", "report") and not releases:
        raise ConfigError(f"{where}: a {policy} app needs releases")
    if releases in HOST:
        if policy != "report" or containers:
            raise ConfigError(f"{where}: {HOST[releases]}, a report app with no containers")
    elif not containers:
        raise ConfigError(f"{where}.containers is empty")
    if policy == "semver" and releases.startswith("hub:"):
        raise ConfigError(f"{where}: a semver app follows GitHub releases")
    minor_wait_days = table.get("minor_wait_days", MINOR_WAIT_DAYS)
    if isinstance(minor_wait_days, bool) or not isinstance(minor_wait_days, int) or minor_wait_days < 0:
        raise ConfigError(f"{where}.minor_wait_days must be a whole number of days, 0 or more")
    if "minor_wait_days" in table and policy != "semver":
        raise ConfigError(f"{where}.minor_wait_days is for semver apps")
    image = normalize_image(_text(where, table, "image")).removeprefix("library/")
    if image and (policy != "floating" or len(containers) != 1):
        raise ConfigError(f"{where}.image is for a floating app with one container")
    if image and (":" not in image.rsplit("/", 1)[-1] or "@" in image):
        raise ConfigError(f"{where}.image must be an image and tag with no digest, such as traefik:v3")
    version_file = _text(where, table, "version_file")
    command = table.get("version_command", [])
    if not isinstance(command, list) or not all(isinstance(part, str) and part for part in command):
        raise ConfigError(f"{where}.version_command must be a list of strings")
    if (version_file or command) and policy != "floating":
        raise ConfigError(f"{where}: version_file and version_command are for floating apps")
    if version_file and command:
        raise ConfigError(f"{where}: version_file or version_command, not both")
    if version_file and not version_file.startswith("/"):
        raise ConfigError(f"{where}.version_file must be an absolute path in the container")
    return App(
        key, title, policy, containers, members, releases, _text(where, table, "tag_prefix"),
        image, version_file, tuple(command), minor_wait_days,
    )


def _text(where: str, table: dict, name: str, required: bool = False) -> str:
    value = table.get(name, "")
    if not isinstance(value, str) or (required and not value.strip()):
        raise ConfigError(f"{where}.{name} must be {'a non-empty' if required else 'a'} string")
    return value.strip()


def _names(where: str, table: dict, name: str) -> tuple[str, ...]:
    value = table.get(name, [])
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ConfigError(f"{where}.{name} must be a list of container names")
    names = tuple(item.strip() for item in value)
    if len(set(names)) != len(names):
        raise ConfigError(f"{where}.{name} names a container twice")
    return names
