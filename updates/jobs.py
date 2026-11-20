"""What runs this morning. Which apps, and how, is config.toml; decisions stay here; Docker and mail stay elsewhere."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import json
import os
import re

from config import App, Config
from mail import Bump, Issue, Notice
from releases import Firmware, choose, github_releases, hub_tags, latest_stable, newer, normalize_image, parse_version, plex_public, qts_public, wait_note
from runtime import Container, Docker, DockerError, Stack, log, split_ref

KINDS = {
    "floating": "updates this morning if the floating tag moved",
    "semver": "patch now, a new minor waits a week",
    "report": "pinned, email if a newer release exists",
}
HOLD_NOTE = "Unused images are kept until updates/state/prune.hold is deleted."
PLEX_DOWNLOADS = "https://www.plex.tv/media-server-downloads/"
QTS_DOWNLOADS = "https://www.qnap.com/en/download"


@dataclass
class Outcome:
    issues: list[Issue] = field(default_factory=list)
    notices: list[Notice] = field(default_factory=list)
    bumps: list[Bump] = field(default_factory=list)
    # (container, image ID) pairs that were rolled back, and (app, tag) of semver updates that failed to start.
    rejected: list[tuple[str, str]] = field(default_factory=list)
    # A restore failed, so the previous image must survive the weekly prune.
    hold: list[str] = field(default_factory=list)
    # (app, error) for release checks that could not be read this morning.
    unchecked: list[tuple[str, str]] = field(default_factory=list)


def classify(config: Config, names: set[str]) -> None:
    kinds: dict[str, str] = {}
    for app in config.apps.values():
        kinds.update({name: _kind(app) for name in app.containers})
        kinds.update({name: f"part of {app.title}" for name in app.members})
    log("running containers:")
    for name in sorted(names):
        log(f"  {name} {kinds.get(name, 'left alone')}")


def _kind(app: App) -> str:
    if app.policy != "semver":
        return KINDS[app.policy]
    kind = "patch now, a new minor is mailed" if app.pin_minor else KINDS["semver"]
    return f"{kind}, rolled back if not healthy" if app.rollback else kind


def run(docker: Docker, config: Config, dry: bool, rejected: set[tuple[str, str]] | frozenset = frozenset(), outcome: Outcome | None = None) -> Outcome:
    """Fill `outcome` as each step finishes, so a caller that is stopped midway keeps what already moved."""
    outcome = outcome if outcome is not None else Outcome()
    try:
        running = docker.names()
    except DockerError as exc:
        outcome.issues.append(Issue("docker", f"Docker could not be reached. Nothing was updated. {exc}"))
        return outcome
    classify(config, running)
    steps, last = [], []
    for app in config.apps.values():
        if app.policy == "report":
            continue
        update = update_semver if app.policy == "semver" else update_floating
        step = (app.key, lambda app=app, update=update: update(docker, config, running, dry, outcome, rejected, app))
        (last if config.compose_container in app.containers else steps).append(step)
    steps.append(("updater", lambda: collect_pinned(docker, config, running, outcome.notices, outcome.unchecked)))
    for key, step in steps + last:
        try:
            step()
        except Exception as exc:
            log(f"{key} stopped: {exc}")
            outcome.issues.append(Issue(key, f"Stopped with an unexpected error. {exc}"))
    return outcome


def update_floating(docker: Docker, config: Config, running: set[str], dry: bool, outcome: Outcome, rejected, app: App) -> None:
    if not _all_running(app, running):
        return
    infos = [docker.inspect(name) for name in app.containers]
    refs = [info.image.split("@", 1)[0] for info in infos]
    if app.image and normalize_image(refs[0]).removeprefix("library/") != app.image:
        outcome.issues.append(Issue(app.key, f"{app.main} runs {refs[0]}, not {app.image}. It was not updated."))
        return
    stack = _stack(outcome, app, infos)
    if stack is None:
        return
    pulled = " and ".join(refs)
    if dry:
        log(f"would pull {pulled} and recreate {' and '.join(app.containers)}")
        return
    services = [info.service for info in infos]
    one = len(infos) == 1
    try:
        log(f"pulling {pulled}")
        docker.compose(stack, "pull", *services)
        new_ids = [docker.image_id(ref) for ref in refs]
    except DockerError as exc:
        unchanged = "container was" if one else "containers were"
        outcome.issues.append(Issue(app.key, f"Pull of {pulled} failed. The running {unchanged} not changed. {exc}"))
        return
    if new_ids == [info.image_id for info in infos]:
        log(f"{app.key} already current")
        return
    candidates = [(info.name, new) for info, new in zip(infos, new_ids) if new != info.image_id]
    if any(item in rejected for item in candidates):
        log(f"{app.key}: this image was rolled back before, waiting for a newer one")
        for info, ref in zip(infos, refs):
            docker.tag(info.image_id, ref)
        return
    before = _version(docker, app, infos[0])

    def recreate() -> None:
        if config.compose_container in app.containers:
            docker.recreate_runner(refs[0], stack)
        else:
            docker.compose(stack, "up", "-d", "--pull", "never", "--no-deps", *services)

    def ready() -> bool:
        return all(docker.wait_ready(name) for name in app.containers)

    def restore() -> bool:
        try:
            for info, ref in zip(infos, refs):
                docker.tag(info.image_id, ref)
            recreate()
        except DockerError:
            return False
        return ready()

    def healthy() -> bool:
        return ready() and all(_runs(docker, info.name, new) for info, new in zip(infos, new_ids))

    if _apply(outcome, app.key, candidates, recreate, restore, "The new image" if one else "The new images", healthy):
        log(f"{app.key} updated")
        _bumped(docker, outcome.bumps, app, before)


def semver_plan(docker: Docker, running: set[str], app: App, unchecked: list[tuple[str, str]] | None = None):
    """Return (containers, running version, tag to install, minor still waiting), or None."""
    if not _all_running(app, running):
        return None
    infos = [docker.inspect(name) for name in app.containers]
    current = infos[0].version
    for info in infos[1:]:
        current = _lower(current, info.version)
    try:
        releases = _releases(app)
    except Exception as exc:
        log(f"could not read {app.title} releases: {exc}")
        if unchecked is not None:
            unchecked.append((app.key, str(exc)))
        return None
    tag, held = choose(current, releases, datetime.now(timezone.utc), app.pin_minor)
    return infos, current, tag, held


def semver_waiting(docker: Docker, running: set[str], app: App, notices: list[Notice]) -> None:
    plan = semver_plan(docker, running, app)
    if plan is None:
        return
    _infos, current, tag, held = plan
    if held and not tag:
        _waiting_notice(app, current, held, notices)


def update_semver(docker: Docker, config: Config, running: set[str], dry: bool, outcome: Outcome, rejected, app: App) -> None:
    plan = semver_plan(docker, running, app, outcome.unchecked)
    if plan is None:
        return
    infos, current, tag, held = plan
    running_version = current.lstrip("v")
    if held and not tag:
        _waiting_notice(app, current, held, outcome.notices)
        return
    if not tag:
        log(f"{app.key} stays on {running_version}")
        return
    version = app.tag_prefix + tag.removeprefix("v")
    no_rollback = f"{app.title} was not rolled back, because its database may already be migrated."
    if (app.key, version) in rejected:
        log(f"{app.key} {version} failed to start before, waiting for a newer release")
        return
    log(f"{app.key} {running_version} -> {version.lstrip('v')}")
    stack = _stack(outcome, app, infos)
    if stack is None:
        return
    # The floating tag compose uses (`:v3` for Immich) comes from the running containers, never from a pull.
    targets = {info.name: (info.image_id, info.image.split("@", 1)[0]) for info in infos}
    if dry:
        return
    try:
        saved = {path: docker.archive(app.main, path) for path in app.rollback_files}
    except DockerError as exc:
        outcome.issues.append(Issue(app.key, f"{version} was not installed: {app.main} could not be backed up first. {exc}"))
        return
    pulled = {}
    refs = {name: f"{split_ref(target)[0]}:{version}" for name, (_old, target) in targets.items()}
    try:
        for name, ref in refs.items():
            log(f"pulling {ref}")
            docker.pull(ref)
            pulled[name] = docker.image_id(ref)
        for name, ref in refs.items():
            docker.tag(ref, targets[name][1])
    except DockerError as exc:
        for old, target in targets.values():
            try:
                docker.tag(old, target)
            except DockerError:
                pass
        outcome.issues.append(Issue(app.key, f"Pull of {version} failed. No container was changed. {exc}"))
        return
    bump = Bump(app.key, running_version, version.lstrip("v"), release_url(app, version))
    if app.rollback:
        if _semver_rollback(docker, outcome, app, stack, [info.service for info in infos], targets, pulled, saved, version):
            log(f"{app.key} updated to {version}")
            outcome.bumps.append(bump)
        return
    try:
        docker.compose(stack, "up", "-d", "--pull", "never", "--no-deps", *(info.service for info in infos))
    except DockerError as exc:
        outcome.rejected.append((app.key, version))
        outcome.issues.append(Issue(app.key, _sentence(f"Recreate to {version} failed: {exc}") + f" {no_rollback}"))
        return
    if not all(_runs(docker, name, image) for name, image in pulled.items()):
        tags = ", ".join(sorted({split_ref(target)[1] for _old, target in targets.values()}))
        outcome.issues.append(Issue(app.key, f"Compose did not start the {version} images. Its files must keep the tag the containers run ({tags}). {no_rollback}"))
        return
    if not all(docker.wait_ready(name) for name in pulled):
        outcome.issues.append(Issue(app.key, f"{version} did not become healthy. {no_rollback}"))
        outcome.bumps.append(bump)
        return
    log(f"{app.key} updated to {version}")
    outcome.bumps.append(bump)


def _semver_rollback(docker: Docker, outcome: Outcome, app: App, stack: Stack, services: list[str], targets, pulled, saved: dict[str, bytes], version: str) -> bool:
    """Start `version`; if it does not come up healthy, start the previous images again with the saved files put back first."""

    def recreate() -> None:
        docker.compose(stack, "up", "-d", "--pull", "never", "--no-deps", *services)

    def healthy() -> bool:
        return all(_runs(docker, name, image) and docker.wait_ready(name) for name, image in pulled.items())

    def restore() -> bool:
        try:
            for old, target in targets.values():
                docker.tag(old, target)
            if saved:
                docker.compose(stack, "up", "--no-start", "--pull", "never", "--no-deps", *services)
                for path, data in saved.items():
                    docker.put_archive(app.main, path, data)
                docker.compose(stack, "start", *services)
            else:
                recreate()
        except DockerError as exc:
            log(f"{app.key} restore failed: {exc}")
            return False
        return all(docker.wait_ready(name) and _runs(docker, name, old) for name, (old, _target) in targets.items())

    return _apply(outcome, app.key, [(app.key, version)], recreate, restore, version, healthy)


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


def release_url(app: App, version: str) -> str:
    """The release page of a version, or the list of releases when the version is a tag like `v3` or `latest`."""
    kind, _, repository = app.releases.partition(":")
    if kind == "hub":
        return _hub_page(repository)
    if kind != "github":
        return ""
    base = f"https://github.com/{repository}/releases"
    text = version.strip()
    if parse_version(text) is None:
        return base
    return f"{base}/tag/{app.tag_prefix}{text.removeprefix('v')}"


def collect_notices(docker: Docker, config: Config, running: set[str], notices: list[Notice]) -> None:
    """Pinned releases, Plex, and a semver minor that is still waiting. Changes nothing."""
    collect_pinned(docker, config, running, notices)
    for app in config.apps.values():
        if app.policy != "semver":
            continue
        try:
            semver_waiting(docker, running, app, notices)
        except Exception as exc:
            log(f"could not check {app.title}: {exc}")


def collect_pinned(docker: Docker, config: Config, running: set[str], notices: list[Notice], unchecked: list[tuple[str, str]] | None = None) -> None:
    for app in config.apps.values():
        if app.policy != "report":
            continue
        if app.releases == "plex":
            _plex(app, notices, unchecked)
            continue
        if app.releases == "qts":
            _qts(app, notices, unchecked)
            continue
        try:
            _release_notice(docker, running, notices, app)
        except Exception as exc:
            log(f"could not check {app.title}: {exc}")
            if unchecked is not None:
                unchecked.append((app.key, str(exc)))


def _all_running(app: App, running: set[str]) -> bool:
    missing = [name for name in app.containers if name not in running]
    if len(missing) == len(app.containers):
        log(f"{app.key} is not running, skipped")
    elif missing:
        log(f"{app.key}: {' and '.join(missing)} not running, skipped")
    return not missing


def _stack(outcome: Outcome, app: App, infos: list[Container]) -> Stack | None:
    """The one Compose project the app's containers were started from."""
    for info in infos:
        if info.stack is None or not info.service:
            outcome.issues.append(Issue(app.key, f"{info.name} was not started by Compose. It was not updated."))
            return None
    if len({info.stack for info in infos}) > 1:
        outcome.issues.append(Issue(app.key, f"{' and '.join(app.containers)} are in different Compose projects. Nothing was updated."))
        return None
    return infos[0].stack


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


def _releases(app: App):
    kind, _, repository = app.releases.partition(":")
    return github_releases(repository) if kind == "github" else hub_tags(repository)


def _release_page(app: App, tag: str) -> str:
    kind, _, repository = app.releases.partition(":")
    return f"https://github.com/{repository}/releases/tag/{tag}" if kind == "github" else _hub_page(repository)


def _hub_page(repository: str) -> str:
    if repository.startswith("library/"):
        return f"https://hub.docker.com/_/{repository.removeprefix('library/')}/tags"
    return f"https://hub.docker.com/r/{repository}/tags"


def _waiting_notice(app: App, current: str, held, notices: list[Notice]) -> None:
    running_version = current.lstrip("v")
    available = held.tag.lstrip("v")
    notices.append(Notice(app.key, running_version, available, _release_page(app, held.tag)))
    note = f"{held.tag} is a new minor, installed by hand" if app.pin_minor else wait_note(held)
    log(f"{app.key} {running_version} -> {available}, {note}")


def _bumped(docker: Docker, bumps: list[Bump], app: App, before: str) -> None:
    try:
        after = _version(docker, app, docker.inspect(app.main))
    except DockerError:
        after = ""
    bumps.append(Bump(app.key, before, after, release_url(app, after)))


def _version(docker: Docker, app: App, info: Container) -> str:
    """The image label, or what the app itself reports, for images that carry no version label."""
    if parse_version(info.version) or not (app.version_file or app.version_command):
        return info.version
    try:
        if app.version_file:
            found = str(json.loads(docker.read_file(info.name, app.version_file)).get("version") or "")
        else:
            code, output = docker.exec(info.name, list(app.version_command))
            match = re.search(r"version v?(\d+(?:\.\d+)+)", output) if code == 0 else None
            found = match.group(1) if match else ""
    except (DockerError, ValueError, AttributeError) as exc:
        log(f"could not read the {info.name} version: {exc}")
        return info.version
    return found if parse_version(found) else info.version


def _release_notice(docker: Docker, running: set[str], notices: list[Notice], app: App) -> None:
    if app.main not in running:
        return
    current = docker.inspect(app.main).version
    found = latest_stable(_releases(app))
    current_version = parse_version(current)
    if found is None or current_version is None or not newer(found.version, current_version):
        log(f"{app.title} {current} is current" if found else f"no stable release found for {app.title}")
        return
    notices.append(Notice(app.key, current, found.tag, _release_page(app, found.tag)))
    log(f"{app.title} {current} -> {found.tag} (email)")


def _plex(app: App, notices: list[Notice], unchecked: list[tuple[str, str]] | None = None) -> None:
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
            unchecked.append((app.key, str(exc)))
        return
    available = found.tag.split("-", 1)[0] if found else ""
    left, right = parse_version(running), parse_version(available)
    if found is None or left is None or right is None or not newer(right, left):
        log(f"Plex {running or current} is current")
        return
    notices.append(Notice(app.key, running, available, PLEX_DOWNLOADS))
    log(f"Plex {running} -> {available} (email)")


def _qts(app: App, notices: list[Notice], unchecked: list[tuple[str, str]] | None = None) -> None:
    """The NAS firmware. update.sh reads the running build and the Live Update feed QTS itself uses."""
    running = Firmware(*(os.environ.get(f"QTS_{name}", "").strip() for name in ("VERSION", "NUMBER", "BUILD")))
    feed = os.environ.get("QTS_FEED", "").strip()
    model = os.environ.get("QTS_MODEL", "").strip()
    if not running.version:
        log("QTS version not read, skipped")
        return
    if running.order is None or not feed.startswith("https://") or not model:
        log(f"QTS {running.label}: the version, feed or model could not be read")
        return
    try:
        found = qts_public(feed, model)
    except Exception as exc:
        log(f"could not check QTS: {exc}")
        if unchecked is not None:
            unchecked.append((app.key, str(exc)))
        return
    if found is None or found.order is None:
        log(f"could not check QTS: no firmware for {model} in {feed}")
        if unchecked is not None:
            unchecked.append((app.key, f"no firmware for {model}"))
        return
    if found.order <= running.order:
        log(f"QTS {running.label} is current")
        return
    notices.append(Notice(app.key, running.label, found.label, found.url or QTS_DOWNLOADS))
    log(f"QTS {running.label} -> {found.label} (email)")


def _lower(left: str, right: str) -> str:
    parsed_left, parsed_right = parse_version(left), parse_version(right)
    if parsed_left and parsed_right and parsed_left <= parsed_right:
        return left
    return right
