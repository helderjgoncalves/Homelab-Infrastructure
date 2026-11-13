"""What runs this morning. Decisions stay here; Docker and mail stay elsewhere."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import json
import os
import re

from mail import Bump, Issue, Notice, release_url
from releases import choose, github_releases, hub_tags, latest_stable, newer, normalize_image, parse_version, plex_public, wait_note
from runtime import STACKS, Docker, DockerError, log

FLOATING = (
    ("cloudflared", "cloudflared/docker-compose.yml", "cloudflare-tunnel", "cloudflare-tunnel", None),
    ("traefik", "traefik/docker-compose.yml", "traefik", "traefik", "traefik:v3"),
    ("uptime-kuma", "uptime-kuma/docker-compose.yml", "uptime-kuma", "uptime-kuma", None),
)
GITHUB = (
    ("adguard", "AdguardTeam/AdGuardHome", "AdGuard Home"),
    ("homeassistant", "home-assistant/core", "Home Assistant"),
    ("zigbee2mqtt", "Koenkk/zigbee2mqtt", "Zigbee2MQTT"),
    ("opencloud-opencloud-1", "opencloud-eu/opencloud", "OpenCloud"),
)
HUB = (
    ("mosquitto", "library/eclipse-mosquitto", "Mosquitto"),
    ("opencloud-collabora-1", "collabora/code", "Collabora"),
    ("opencloud-yjs-1", "opencloudeu/yjs", "Yjs"),
)
QUIET = {"opencloud-collabora-1", "opencloud-yjs-1"}
IGNORED = {"devbox", "ntfy"}
PINNED = {name for name, *_rest in (*GITHUB, *HUB)}
IMMEDIATE = {"traefik", "cloudflare-tunnel", "beszel", "beszel-agent", "uptime-kuma", "dockge"}
IMMICH = (
    ("immich_server", "immich-server", "ghcr.io/immich-app/immich-server"),
    ("immich_machine_learning", "immich-machine-learning", "ghcr.io/immich-app/immich-machine-learning"),
)
APP_VERSION = {"uptime-kuma": "/app/package.json", "dockge": "/app/package.json"}
VERSION_COMMAND = {"cloudflare-tunnel": ["cloudflared", "--version"]}
HOLD_NOTE = "Unused images are kept until updates/state/prune.hold is deleted."
NO_ROLLBACK = "Immich was not rolled back, because its database may already be migrated."


@dataclass
class Outcome:
    issues: list[Issue] = field(default_factory=list)
    notices: list[Notice] = field(default_factory=list)
    bumps: list[Bump] = field(default_factory=list)
    # (container, image ID) pairs that were rolled back, and Immich versions that failed to start.
    rejected: list[tuple[str, str]] = field(default_factory=list)
    # A restore failed, so the previous image must survive the weekly prune.
    hold: list[str] = field(default_factory=list)
    # (main container, error) for release checks that could not be read this morning.
    unchecked: list[tuple[str, str]] = field(default_factory=list)


def classify(names: set[str]) -> None:
    log("running containers:")
    for name in sorted(names):
        if name in IGNORED:
            kind = "ignored"
        elif name in IMMEDIATE:
            kind = "updates this morning if the floating tag moved"
        elif name in {"immich_server", "immich_machine_learning"}:
            kind = "patch now, a new minor waits a week"
        elif name in {"immich_postgres", "immich_redis"}:
            kind = "part of Immich"
        elif name in QUIET:
            kind = "part of OpenCloud"
        elif name in PINNED:
            kind = "pinned, email if a newer release exists"
        else:
            kind = "left alone"
        log(f"  {name} {kind}")


def run(docker: Docker, dry: bool, rejected: set[tuple[str, str]] | frozenset = frozenset(), outcome: Outcome | None = None) -> Outcome:
    """Fill `outcome` as each step finishes, so a caller that is stopped midway keeps what already moved."""
    outcome = outcome if outcome is not None else Outcome()
    try:
        running = docker.names()
    except DockerError as exc:
        outcome.issues.append(Issue("docker", f"Docker could not be reached. Nothing was updated. {exc}"))
        return outcome
    classify(running)
    steps = [("immich", lambda: update_immich(docker, running, dry, outcome, rejected))]
    for project, relative, service, container, expect in FLOATING:
        steps.append((container, lambda project=project, relative=relative, service=service, container=container, expect=expect: update_one(
            docker, running, dry, outcome, rejected, project, STACKS / relative, service, container, expect,
        )))
    steps.append(("beszel", lambda: update_beszel(docker, running, dry, outcome, rejected)))
    steps.append(("updater", lambda: collect_pinned(docker, running, outcome.notices, outcome.unchecked)))
    steps.append(("dockge", lambda: update_dockge(docker, running, dry, outcome, rejected)))
    for key, step in steps:
        try:
            step()
        except Exception as exc:
            log(f"{key} stopped: {exc}")
            outcome.issues.append(Issue(key, f"Stopped with an unexpected error. {exc}"))
    return outcome


def update_one(docker: Docker, running: set[str], dry: bool, outcome: Outcome, rejected, project: str, file: Path, service: str, container: str, expect: str | None = None) -> None:
    if container not in running:
        log(f"{container} is not running, skipped")
        return
    info = docker.inspect(container)
    ref = info.image.split("@", 1)[0]
    if expect and normalize_image(ref).removeprefix("library/") != expect:
        outcome.issues.append(Issue(container, f"{container} runs {ref}, not {expect}. It was not updated."))
        return
    if dry:
        log(f"would pull {ref} and recreate {container}")
        return
    try:
        log(f"pulling {ref}")
        docker.compose(project, file, "pull", service)
        new_id = docker.image_id(ref)
    except DockerError as exc:
        outcome.issues.append(Issue(container, f"Pull of {ref} failed. The running container was not changed. {exc}"))
        return
    if new_id == info.image_id:
        log(f"{container} already current")
        return
    if (container, new_id) in rejected:
        log(f"{container}: this image was rolled back before, waiting for a newer one")
        docker.tag(info.image_id, ref)
        return
    before = _version(docker, info)
    if _apply(
        outcome, container, [(container, new_id)],
        lambda: docker.compose(project, file, "up", "-d", "--pull", "never", "--no-deps", service),
        lambda: _restore_one(docker, container, ref, info.image_id, file, project, service),
        "The new image",
        lambda: docker.wait_ready(container) and _runs(docker, container, new_id),
    ):
        log(f"{container} updated")
        _bumped(docker, outcome.bumps, container, before)


def update_beszel(docker: Docker, running: set[str], dry: bool, outcome: Outcome, rejected) -> None:
    if "beszel" not in running or "beszel-agent" not in running:
        missing_both = "beszel" not in running and "beszel-agent" not in running
        log("beszel is not running, skipped" if missing_both else "beszel hub and agent are not both running, skipped")
        return
    file = STACKS / "beszel/docker-compose.yml"
    if dry:
        log("would pull henrygd/beszel:0 and henrygd/beszel-agent:0 and recreate both")
        return
    hub, agent = docker.inspect("beszel"), docker.inspect("beszel-agent")
    hub_ref, agent_ref = hub.image.split("@", 1)[0], agent.image.split("@", 1)[0]

    def recreate() -> None:
        docker.compose("beszel", file, "up", "-d", "--pull", "never", "--no-deps", "beszel", "beszel-agent")

    try:
        log("pulling beszel and beszel-agent")
        docker.compose("beszel", file, "pull", "beszel", "beszel-agent")
        hub_new, agent_new = docker.image_id(hub_ref), docker.image_id(agent_ref)
    except DockerError as exc:
        outcome.issues.append(Issue("beszel", f"Pull failed. The running containers were not changed. {exc}"))
        return
    if hub_new == hub.image_id and agent_new == agent.image_id:
        log("beszel already current")
        return
    candidates = [(name, new) for name, new, old in (("beszel", hub_new, hub.image_id), ("beszel-agent", agent_new, agent.image_id)) if new != old]
    if any(item in rejected for item in candidates):
        log("beszel: this image was rolled back before, waiting for a newer one")
        docker.tag(hub.image_id, hub_ref)
        docker.tag(agent.image_id, agent_ref)
        return

    def restore() -> bool:
        try:
            docker.tag(hub.image_id, hub_ref)
            docker.tag(agent.image_id, agent_ref)
            recreate()
        except DockerError:
            return False
        return docker.wait_ready("beszel") and docker.wait_ready("beszel-agent")

    def healthy() -> bool:
        return (
            docker.wait_ready("beszel") and docker.wait_ready("beszel-agent")
            and _runs(docker, "beszel", hub_new) and _runs(docker, "beszel-agent", agent_new)
        )

    if _apply(outcome, "beszel", candidates, recreate, restore, "The new images", healthy):
        log("beszel updated")
        _bumped(docker, outcome.bumps, "beszel", hub.version)


def immich_plan(docker: Docker, running: set[str], unchecked: list[tuple[str, str]] | None = None):
    """Return (server, machine learning, running version, tag to install, minor still waiting), or None."""
    present = {"immich_server", "immich_machine_learning"} & running
    if not present:
        log("immich is not running, skipped")
        return None
    if present != {"immich_server", "immich_machine_learning"}:
        log("immich server and machine-learning are not both running, skipped")
        return None
    server, learning = docker.inspect("immich_server"), docker.inspect("immich_machine_learning")
    current = _lower(server.version, learning.version)
    try:
        releases = github_releases("immich-app/immich")
    except Exception as exc:
        log(f"could not read Immich releases: {exc}")
        if unchecked is not None:
            unchecked.append(("immich", str(exc)))
        return None
    tag, held = choose(current, releases, datetime.now(timezone.utc))
    return server, learning, current, tag, held


def immich_waiting(docker: Docker, running: set[str], notices: list[Notice]) -> None:
    plan = immich_plan(docker, running)
    if plan is None:
        return
    _server, _learning, current, tag, held = plan
    if held and not tag:
        _immich_notice(current, held, notices)


def update_immich(docker: Docker, running: set[str], dry: bool, outcome: Outcome, rejected) -> None:
    plan = immich_plan(docker, running, outcome.unchecked)
    if plan is None:
        return
    server, learning, current, tag, held = plan
    running_version = current.lstrip("v")
    if held and not tag:
        _immich_notice(current, held, outcome.notices)
        return
    if not tag:
        log(f"immich stays on {running_version}")
        return
    version = tag if tag.startswith("v") else f"v{tag}"
    if ("immich", version) in rejected:
        log(f"immich {version} failed to start before, waiting for a newer release")
        return
    log(f"immich {running_version} -> {version.lstrip('v')}")
    # The floating tag compose uses (`:v3` today) comes from the running container, never from a pull.
    targets = {}
    for info, (container, _service, repository) in zip((server, learning), IMMICH):
        target = info.image.split("@", 1)[0]
        if target.rpartition(":")[0] != repository:
            outcome.issues.append(Issue("immich", f"{container} runs {target}, not {repository}. Immich was not updated."))
            return
        targets[container] = (info.image_id, target)
    if dry:
        return
    pulled = {}
    try:
        for container, _service, repository in IMMICH:
            ref = f"{repository}:{version}"
            log(f"pulling {ref}")
            docker.pull(ref)
            pulled[container] = docker.image_id(ref)
        for container, _service, repository in IMMICH:
            docker.tag(f"{repository}:{version}", targets[container][1])
    except DockerError as exc:
        for old, target in targets.values():
            try:
                docker.tag(old, target)
            except DockerError:
                pass
        outcome.issues.append(Issue("immich", f"Pull of {version} failed. Neither container was changed. {exc}"))
        return
    file = STACKS / "immich/docker-compose.yml"
    try:
        docker.compose("immich", file, "up", "-d", "--pull", "never", "--no-deps", *(service for _container, service, _repository in IMMICH))
    except DockerError as exc:
        outcome.rejected.append(("immich", version))
        outcome.issues.append(Issue("immich", _sentence(f"Recreate to {version} failed: {exc}") + f" {NO_ROLLBACK}"))
        return
    if not all(_runs(docker, container, image) for container, image in pulled.items()):
        outcome.issues.append(Issue("immich", f"Compose did not start the {version} images. Check IMMICH_VERSION in immich/.env. {NO_ROLLBACK}"))
        return
    bump = Bump("immich", running_version, version.lstrip("v"), release_url("immich", version))
    if not all(docker.wait_ready(container) for container in pulled):
        outcome.issues.append(Issue("immich", f"{version} did not become healthy. {NO_ROLLBACK}"))
        outcome.bumps.append(bump)
        return
    log(f"immich updated to {version}")
    outcome.bumps.append(bump)


def update_dockge(docker: Docker, running: set[str], dry: bool, outcome: Outcome, rejected) -> None:
    if "dockge" not in running:
        log("dockge is not running, skipped")
        return
    info = docker.inspect("dockge")
    ref = info.image.split("@", 1)[0]
    file = STACKS / "dockge/compose.yaml"
    if dry:
        log(f"would pull {ref} and recreate dockge")
        return
    try:
        log(f"pulling {ref}")
        docker.compose("dockge", file, "pull", "dockge")
        new_id = docker.image_id(ref)
    except DockerError as exc:
        outcome.issues.append(Issue("dockge", f"Pull of {ref} failed. The running container was not changed. {exc}"))
        return
    if new_id == info.image_id:
        log("dockge already current")
        return
    if ("dockge", new_id) in rejected:
        log("dockge: this image was rolled back before, waiting for a newer one")
        docker.tag(info.image_id, ref)
        return
    before = _version(docker, info)
    if _apply(
        outcome, "dockge", [("dockge", new_id)],
        lambda: docker.recreate_dockge(ref, file),
        lambda: _restore_one(docker, "dockge", ref, info.image_id, file, "dockge", "dockge", dockge=True),
        "The new image",
        lambda: docker.wait_ready("dockge") and _runs(docker, "dockge", new_id),
    ):
        log("dockge updated")
        _bumped(docker, outcome.bumps, "dockge", before)


def apply_update(recreate, restore, label: str, healthy) -> tuple[str | None, bool]:
    """Return (failure text, previous image restored). (None, False) when the update held."""
    try:
        recreate()
    except DockerError as exc:
        failure = _sentence(f"Recreate failed: {exc}")
    else:
        if healthy():
            return None, False
        failure = f"{label} did not become healthy."
    if restore():
        return f"{failure} The previous image was restored.", True
    return f"{failure} Restoring the previous image failed.", False


def _apply(outcome: Outcome, key: str, candidates: list[tuple[str, str]], recreate, restore, label: str, healthy) -> bool:
    detail, restored = apply_update(recreate, restore, label, healthy)
    if detail is None:
        return True
    if restored:
        outcome.rejected.extend(candidates)
    else:
        outcome.hold.append(f"{key}\t{detail}")
        detail = f"{detail} {HOLD_NOTE}"
    outcome.issues.append(Issue(key, detail))
    return False


def _runs(docker: Docker, container: str, image_id: str) -> bool:
    try:
        return docker.inspect(container).image_id == image_id
    except DockerError:
        return False


def _sentence(text: str) -> str:
    text = text.strip()
    return text if text.endswith(".") else f"{text}."


def _immich_notice(current: str, held, notices: list[Notice]) -> None:
    running_version = current.lstrip("v")
    available = held.tag.lstrip("v")
    notices.append(Notice("immich", running_version, available, f"https://github.com/immich-app/immich/releases/tag/{held.tag}"))
    log(f"immich {running_version} -> {available}, {wait_note(held)}")


def _bumped(docker: Docker, bumps: list[Bump], key: str, before: str) -> None:
    try:
        after = _version(docker, docker.inspect(key))
    except DockerError:
        after = ""
    bumps.append(Bump(key, before, after, release_url(key, after)))


def _version(docker: Docker, info) -> str:
    """The image label, or what the app itself reports, for images that carry no version label."""
    if parse_version(info.version) or (info.name not in APP_VERSION and info.name not in VERSION_COMMAND):
        return info.version
    try:
        if info.name in APP_VERSION:
            found = str(json.loads(docker.read_file(info.name, APP_VERSION[info.name])).get("version") or "")
        else:
            code, output = docker.exec(info.name, VERSION_COMMAND[info.name])
            match = re.search(r"version v?(\d+(?:\.\d+)+)", output) if code == 0 else None
            found = match.group(1) if match else ""
    except (DockerError, ValueError, AttributeError) as exc:
        log(f"could not read the {info.name} version: {exc}")
        return info.version
    return found if parse_version(found) else info.version


def _restore_one(docker: Docker, container: str, ref: str, old: str, file: Path, project: str, service: str, dockge: bool = False) -> bool:
    try:
        docker.tag(old, ref)
        if dockge:
            docker.recreate_dockge(ref, file)
        else:
            docker.compose(project, file, "up", "-d", "--pull", "never", "--no-deps", service)
    except DockerError:
        return False
    return docker.wait_ready(container)


def collect_notices(docker: Docker, running: set[str], notices: list[Notice]) -> None:
    """Pinned releases, Plex, and an Immich minor that is still waiting. Changes nothing."""
    collect_pinned(docker, running, notices)
    try:
        immich_waiting(docker, running, notices)
    except Exception as exc:
        log(f"could not check Immich: {exc}")


def collect_pinned(docker: Docker, running: set[str], notices: list[Notice], unchecked: list[tuple[str, str]] | None = None) -> None:
    checks = [
        (container, label, lambda repo=repo: github_releases(repo), f"https://github.com/{repo}/releases/tag/{{tag}}")
        for container, repo, label in GITHUB
    ] + [
        (container, label, lambda repo=repo: hub_tags(repo), "https://hub.docker.com/_/eclipse-mosquitto/tags" if repo.startswith("library/") else f"https://hub.docker.com/r/{repo}/tags")
        for container, repo, label in HUB
    ]
    for container, label, fetch, url in checks:
        try:
            _release_notice(docker, running, notices, container, label, fetch, url)
        except Exception as exc:
            log(f"could not check {label}: {exc}")
            if unchecked is not None and container not in QUIET:
                unchecked.append((container, str(exc)))
    _plex(notices, unchecked)


def _release_notice(docker: Docker, running: set[str], notices: list[Notice], container: str, label: str, fetch, url) -> None:
    if container not in running:
        return
    current = docker.inspect(container).version
    found = latest_stable(fetch())
    current_version = parse_version(current)
    if found is None or current_version is None or not newer(found.version, current_version):
        log(f"{label} {current} is current" if found else f"no stable release found for {label}")
        return
    link = url.format(tag=found.tag) if "{tag}" in url else url
    if container in QUIET:
        log(f"{label} {current} -> {found.tag}, left with the platform")
        return
    notices.append(Notice(container, current, found.tag, link))
    log(f"{label} {current} -> {found.tag} (email)")


def _plex(notices: list[Notice], unchecked: list[tuple[str, str]] | None = None) -> None:
    current = os.environ.get("PLEX_VERSION", "").strip()
    if not current:
        log("plex is not running, skipped")
        return
    running = ""
    for line in reversed(current.splitlines()):
        candidate = line.strip().split("-", 1)[0]
        if parse_version(candidate):
            running = candidate
            break
    if not running:
        log("plex version could not be read")
        return
    try:
        found = plex_public()
    except Exception as exc:
        log(f"could not check Plex: {exc}")
        if unchecked is not None:
            unchecked.append(("plex", str(exc)))
        return
    available = found.tag.split("-", 1)[0] if found else ""
    left, right = parse_version(running), parse_version(available)
    if found is None or left is None or right is None or not newer(right, left):
        log(f"Plex {running or current} is current")
        return
    notices.append(Notice("plex", running, available, "https://www.plex.tv/media-server-downloads/"))
    log(f"Plex {running} -> {available} (email)")


def _lower(left: str, right: str) -> str:
    parsed_left, parsed_right = parse_version(left), parse_version(right)
    if parsed_left and parsed_right and parsed_left <= parsed_right:
        return left
    return right
