"""config.toml: which apps the run updates or reports, and how. Anything it does not understand stops the run."""

from __future__ import annotations

import posixpath
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .releases import normalize_image
from .runtime import HERE

PATH = HERE / "config.toml"
POLICIES = ("floating", "semver", "report")
# Keys the updater itself uses in the mail and in state/.
RESERVED = {"docker", "images", "updater"}
KEY = re.compile(r"[A-Za-z0-9_-]+")
RELEASES = re.compile(r"github:[\w.-]+/[\w.-]+|hub:[\w.-]+/[\w.-]+|plex|qts")
# Releases of the NAS itself, read on the host by update.sh: no containers.
HOST = {"plex": "plex is the QNAP package", "qts": "qts is the NAS firmware"}
AUTO = ("off", "shadow", "on")
# Only for apps with auto: what Workers AI is shown besides the release notes.
AUTO_FIELDS = ("inventory_file", "inventory_key", "inventory_match", "context_files", "linked_notes", "breaking_marker", "breaking_names")
FIELDS = {
    "title", "policy", "containers", "members", "releases", "tag_prefix", "pin_minor", "rollback", "rollback_files",
    "image", "version_file", "version_command", "hold_major", "wait_days", "critical", "auto", "variant", *AUTO_FIELDS,
}


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
    pin_minor: bool = False
    rollback: bool = False
    rollback_files: tuple[str, ...] = ()
    hold_major: bool = False
    # Days a release it would take by itself waits first: a semver minor, or one Workers AI judges.
    wait_days: int = 0
    # A new major is always installed by hand, even with auto. --check flags a tag that would take one anyway.
    critical: bool = False
    # "shadow" or "on"; "" when off.
    auto: str = ""
    inventory_file: str = ""
    inventory_key: str = ""
    inventory_match: str = "{}"
    context_files: tuple[str, ...] = ()
    linked_notes: bool = False
    # A release the tag takes by itself waits for Workers AI when a line of its notes carries breaking_marker and names
    # something in the inventory, or nothing: a change to the app itself. breaking_names is how a line names one.
    breaking_marker: str = ""
    breaking_names: str = ""
    # hub: the tag variant every release is published with (`-alpine`), when the tag the containers run has none.
    variant: str = ""

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
        if runner in app.containers and app.rollback_files:
            raise ConfigError(f"{path.name}: apps.{app.key}: {runner} runs Compose, so it can't be stopped to save rollback_files")
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
    pin_minor = table.get("pin_minor", False)
    rollback = table.get("rollback", False)
    hold_major = table.get("hold_major", False)
    for name, value in (("pin_minor", pin_minor), ("rollback", rollback), ("hold_major", hold_major)):
        if not isinstance(value, bool):
            raise ConfigError(f"{where}.{name} must be true or false")
    if "pin_minor" in table and policy != "semver":
        raise ConfigError(f"{where}.pin_minor is for semver apps")
    if "hold_major" in table and policy != "floating":
        raise ConfigError(f"{where}.hold_major is for floating apps; a semver app never takes another major")
    if "rollback" in table and policy != "semver":
        raise ConfigError(f"{where}.rollback is for semver apps; a floating app always rolls back")
    mode = table.get("auto", "off")
    if mode not in AUTO:
        raise ConfigError(f"{where}.auto must be one of {', '.join(AUTO)}")
    auto = "" if mode == "off" else mode
    rollback_files = _paths(where, table, "rollback_files")
    if rollback_files and not rollback and not (auto and policy == "floating"):
        raise ConfigError(f"{where}.rollback_files needs rollback = true, or auto on a floating app")
    image = normalize_image(_text(where, table, "image")).removeprefix("library/")
    if image and (policy != "floating" or len(containers) != 1):
        raise ConfigError(f"{where}.image is for a floating app with one container")
    if image and (":" not in image.rsplit("/", 1)[-1] or "@" in image):
        raise ConfigError(f"{where}.image must be an image and tag with no digest, such as traefik:v3")
    if auto:
        if not releases.startswith("github:"):
            raise ConfigError(f"{where}.auto needs github: releases, whose notes Workers AI reads")
        if policy == "report":
            raise ConfigError(f"{where}.auto is for floating and semver apps; a report app is never changed")
        if image:
            raise ConfigError(f"{where}: auto changes the tag, and image pins it")
        if policy == "semver" and not rollback:
            raise ConfigError(f"{where}.auto needs rollback = true on a semver app")
    else:
        extra = [name for name in AUTO_FIELDS if name in table]
        if extra:
            raise ConfigError(f"{where}.{extra[0]} needs auto")
    wait_days = table.get("wait_days", 0)
    if isinstance(wait_days, bool) or not isinstance(wait_days, int) or wait_days < 0:
        raise ConfigError(f"{where}.wait_days must be a whole number of days, 0 or more")
    if "wait_days" in table and policy != "semver" and not auto:
        raise ConfigError(f"{where}.wait_days is for semver apps and apps with auto")
    linked_notes = table.get("linked_notes", False)
    critical = table.get("critical", False)
    for name, value in (("linked_notes", linked_notes), ("critical", critical)):
        if not isinstance(value, bool):
            raise ConfigError(f"{where}.{name} must be true or false")
    inventory_file = _text(where, table, "inventory_file")
    inventory_key = _text(where, table, "inventory_key")
    inventory_match = table.get("inventory_match", "{}")
    if inventory_file and not _absolute(inventory_file):
        raise ConfigError(f"{where}.inventory_file must be an absolute path in the container")
    if bool(inventory_file) != bool(inventory_key):
        raise ConfigError(f"{where}: inventory_file and inventory_key go together")
    if "inventory_match" in table and (not isinstance(inventory_match, str) or inventory_match.count("{}") != 1 or not inventory_file):
        raise ConfigError(f"{where}.inventory_match must hold {{}} once, where each inventory_key value goes, and needs inventory_file")
    context_files = _paths(where, table, "context_files")
    breaking_marker = _text(where, table, "breaking_marker")
    breaking_names = table.get("breaking_names", "")
    if breaking_marker and policy != "floating":
        raise ConfigError(f"{where}.breaking_marker is for floating apps; a semver app's minors and majors are already asked about")
    if "breaking_names" in table and (not isinstance(breaking_names, str) or breaking_names.count("{}") != 1 or not breaking_marker or not inventory_file):
        raise ConfigError(f"{where}.breaking_names must hold {{}} once, where a name goes, and needs breaking_marker and inventory_file")
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
    variant = _text(where, table, "variant")
    if variant and (not variant.startswith("-") or len(variant) < 2 or not releases.startswith("hub:")):
        raise ConfigError(f"{where}.variant is a tag suffix such as -alpine, for hub: releases")
    return App(
        key, title, policy, containers, members, releases, _text(where, table, "tag_prefix"),
        image, version_file, tuple(command), pin_minor, rollback, rollback_files, hold_major, wait_days, critical,
        auto, inventory_file, inventory_key, inventory_match, context_files, linked_notes, breaking_marker, breaking_names,
        variant,
    )


def _paths(where: str, table: dict, name: str) -> tuple[str, ...]:
    value = table.get(name, [])
    if not isinstance(value, list) or not all(isinstance(path, str) and _absolute(path) for path in value):
        raise ConfigError(f"{where}.{name} must be a list of absolute paths in the container, below /")
    return tuple(value)


def _absolute(path: str) -> bool:
    """An absolute path below the root, written plainly: rollback_files are cleared before they are put back."""
    return path.startswith("/") and path != "/" and posixpath.normpath(path) == path


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
