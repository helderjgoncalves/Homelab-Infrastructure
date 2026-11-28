"""What runs this morning. Which apps, and how, is config.toml; decisions stay here; Docker and mail stay elsewhere."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import json
import os
import re
from pathlib import Path

import auto
import composefile
import notes
from config import App, Config
from mail import Bump, Issue, Notice
from releases import Firmware, Release, choose, github_releases, hub_tags, latest_stable, newer, normalize_image, parse_version, plex_public, qts_public, wait_note
from runtime import Container, Docker, DockerError, Stack, log, split_ref
from verdict import HELD_AI, HELD_BREAKING, Verdict

KINDS = {
    "floating": "updates this morning if the floating tag moved",
    "semver": "patch now, a new minor waits {days} days",
    "report": "pinned, email if a newer release exists",
}
LEADING_VERSION = re.compile(r"v?(\d+(?:\.\d+)*)")
TAG_LINE = re.compile(r"v?(\d+(?:\.\d+)*)(-[A-Za-z][\w.-]*)?")
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


@dataclass
class Morning:
    """What a move needs. Only the morning run has one, so the Sunday mail and --notify-only never change anything."""
    docker: Docker
    config: Config
    dry: bool
    outcome: Outcome
    rejected: set[tuple[str, str]] | frozenset = frozenset()


def classify(config: Config, names: set[str]) -> None:
    kinds: dict[str, str] = {}
    for app in config.apps.values():
        kinds.update({name: _kind(app) for name in app.containers})
        kinds.update({name: f"part of {app.title}" for name in app.members})
    log("running containers:")
    for name in sorted(names):
        log(f"  {name} {kinds.get(name, 'left alone')}")


def _kind(app: App) -> str:
    if app.policy == "floating" and app.hold_major:
        kind = f"{KINDS['floating']}, a new major is mailed"
    elif app.policy != "semver":
        kind = KINDS[app.policy]
    elif app.pin_minor:
        kind = "patch now, a new minor is mailed"
    else:
        kind = KINDS["semver"].format(days=app.wait_days) if app.wait_days else "patch and new minor now"
    if app.policy == "semver" and app.rollback:
        kind = f"{kind}, rolled back if not healthy"
    if app.auto == "on":
        when = f"after {app.wait_days} days" if app.wait_days else "the first morning"
        return f"{kind}; or installed by itself {when}, if Workers AI finds nothing to do by hand"
    if app.auto == "shadow":
        return f"{kind}; Workers AI says whether it could go in by itself"
    return kind


def explain(app: App, tag: str) -> list[tuple[str, str]]:
    """What each kind of new release does to `app` while it runs `tag`, from its config alone."""
    if app.policy == "report":
        return [("newer release", "emailed, installed by hand")]
    if app.policy == "semver":
        minor = f"by itself after {app.wait_days} days" if app.wait_days else "by itself"
        return [("patch", "by itself"), ("minor", _judged(app, False) if app.pin_minor else minor), ("major", _judged(app, True))]
    line = tag_line(tag)
    size = len(line[0]) if line else 0

    def past(major: bool) -> str:
        return _judged(app, major) if app.releases else "not checked: no releases to compare with"

    alone = "by itself"
    if app.breaking_marker:
        alone = f"by itself, unless its notes mark a breaking change to something in use; then {_judged(app, False)}"
    major = (_judged(app, True) if app.hold_major else alone) if size == 0 else past(True)
    return [("patch", alone if size < 3 else past(False)), ("minor", alone if size < 2 else past(False)), ("major", major)]


def _judged(app: App, major: bool) -> str:
    if not app.auto or (major and app.critical):
        return "emailed, installed by hand"
    wait = f", {app.wait_days} days after it is out" if app.wait_days else ""
    if app.auto == "shadow":
        return f"emailed with Workers AI's verdict{wait} (shadow), installed by hand"
    return f"Workers AI is asked{wait}: by itself if nothing needs doing by hand, else emailed"


def run(docker: Docker, config: Config, dry: bool, rejected: set[tuple[str, str]] | frozenset = frozenset(), outcome: Outcome | None = None,
        judge: auto.Judge | None = None) -> Outcome:
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
        step = (app.key, lambda app=app, update=update: update(docker, config, running, dry, outcome, rejected, app, judge=judge))
        (last if config.compose_container in app.containers else steps).append(step)
    morning = Morning(docker, config, dry, outcome, rejected)
    steps.append(("updater", lambda: collect_pinned(docker, config, running, outcome.notices, outcome.unchecked)))
    steps.append(("updater", lambda: collect_outside(docker, config, running, outcome.notices, outcome.unchecked, judge, morning)))
    for key, step in steps + last:
        try:
            step()
        except Exception as exc:
            log(f"{key} stopped: {exc}")
            outcome.issues.append(Issue(key, f"Stopped with an unexpected error. {exc}"))
    return outcome


def update_floating(docker: Docker, config: Config, running: set[str], dry: bool, outcome: Outcome, rejected, app: App, judge: auto.Judge | None = None) -> None:
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
    if app.hold_major or app.breaking_marker:
        try:
            held = _holds(docker, outcome, app, infos[0], refs[0], judge, infos)
        except Exception as exc:
            # The tag must not point at an image nobody decided on: a redeploy would start it.
            log(f"{app.key}: the new image could not be checked, so it waits for the next morning: {exc}")
            outcome.unchecked.append((app.key, str(exc)))
            held = True
        if isinstance(held, Verdict):
            morning = Morning(docker, config, dry, outcome, rejected)
            pulled = {info.name: new for info, new in zip(infos, new_ids)}
            after = _leading_version(docker.image_version(refs[0]))
            _move(morning, judge, app, infos, stack, {info.name: ref for info, ref in zip(infos, refs)}, lambda: pulled,
                  _leading_version(infos[0].version), after, release_url(app, after), held, candidates)
            return
        if held:
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
    """Return (containers, running version, tag to install, release still waiting, releases), or None."""
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
    tag, held = choose(current, releases, datetime.now(timezone.utc), app.pin_minor, timedelta(days=app.wait_days))
    return infos, current, tag, held, releases


def semver_waiting(docker: Docker, running: set[str], app: App, notices: list[Notice], judge: auto.Judge | None = None) -> None:
    plan = semver_plan(docker, running, app)
    if plan is None:
        return
    infos, current, tag, held, releases = plan
    if held and not tag:
        _held_semver(app, infos, current, held, releases, notices, judge)


def update_semver(docker: Docker, config: Config, running: set[str], dry: bool, outcome: Outcome, rejected, app: App, judge: auto.Judge | None = None) -> None:
    plan = semver_plan(docker, running, app, outcome.unchecked)
    if plan is None:
        return
    infos, current, tag, held, releases = plan
    running_version = current.lstrip("v")
    if held and not tag:
        _held_semver(app, infos, current, held, releases, outcome.notices, judge, Morning(docker, config, dry, outcome, rejected))
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


def collect_notices(docker: Docker, config: Config, running: set[str], notices: list[Notice], judge: auto.Judge | None = None) -> None:
    """Pinned releases, Plex, floating releases outside their tag, and semver releases not installed. Changes nothing."""
    collect_pinned(docker, config, running, notices)
    collect_outside(docker, config, running, notices, None, judge)
    for app in config.apps.values():
        if app.policy != "semver":
            continue
        try:
            semver_waiting(docker, running, app, notices, judge)
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


def collect_outside(docker: Docker, config: Config, running: set[str], notices: list[Notice], unchecked: list[tuple[str, str]] | None = None,
                    judge: auto.Judge | None = None, morning: Morning | None = None) -> None:
    """A floating app's newest release that the tag it runs will never bring: a new minor for `v3.7`, a new major for `2`.

    With auto, Workers AI is asked about it the first morning it is seen, and with auto = "on" that run moves the tag.
    """
    for app in config.apps.values():
        if app.policy != "floating" or not app.releases or app.main not in running:
            continue
        try:
            _outside_notice(docker, notices, app, judge, morning)
        except Exception as exc:
            log(f"could not check {app.title}: {exc}")
            if unchecked is not None:
                unchecked.append((app.key, str(exc)))


def tag_line(tag: str) -> tuple[tuple[int, ...], str] | None:
    """The versions a tag follows and its variant: `v3.7` is ((3, 7), ""), `2.1-alpine` is ((2, 1), "-alpine"). `latest` follows none."""
    match = TAG_LINE.fullmatch(tag)
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split(".")), match.group(2) or ""


def _outside_notice(docker: Docker, notices: list[Notice], app: App, judge: auto.Judge | None = None, morning: Morning | None = None) -> None:
    info = docker.inspect(app.main)
    tag = split_ref(info.image.split("@", 1)[0])[1]
    line = tag_line(tag)
    if line is None:
        return
    prefix, suffix = line
    current = parse_version(_version(docker, app, info))
    if current is None:
        log(f"could not read the {app.main} version, so releases past :{tag} are not checked")
        return
    releases = _releases(app, suffix)
    found = latest_stable(releases)
    if found is None or not newer(found.version, current) or found.version[:len(prefix)] == prefix:
        return
    running_version, available = (".".join(str(part) for part in version) for version in (current, found.version))
    url = _release_page(app, found.tag)
    new_tag = composefile.next_tag(tag, found.version)
    if not app.auto or judge is None or new_tag is None:
        notices.append(Notice(app.key, running_version, available, url))
        log(f"{app.title} {running_version} -> {available} is past :{tag}, installed by hand (email)")
        return
    try:
        infos = [info if name == app.main else docker.inspect(name) for name in app.containers]
    except DockerError as exc:
        notices.append(Notice(app.key, running_version, available, url))
        log(f"{app.title} {running_version} -> {available} is past :{tag}, installed by hand (email): {exc}")
        return
    # The app's other containers on the same tag move with it, like an agent beside its hub.
    images = {item.name: _retag_same(item, tag, new_tag) for item in infos}
    rejected = morning.rejected if morning else frozenset()
    state, verdict = _decide(judge, app, infos, current, found, releases, images, available, len(prefix), rejected)
    if state == "auto" and morning is not None and not morning.dry:

        def fetch() -> dict[str, str]:
            log(f"pulling {' and '.join(images.values())}")
            morning.docker.compose(info.stack, "pull", *(item.service for item in infos))
            return {name: morning.docker.image_id(ref) for name, ref in images.items()}

        _move(morning, judge, app, infos, info.stack, images, fetch, running_version, available, url, verdict, [(app.key, available)])
        return
    notices.append(_judged_notice(app, running_version, available, url, state, verdict, f"is past :{tag}"))


def _retag_same(info: Container, tag: str, new_tag: str) -> str:
    ref = info.image.split("@", 1)[0]
    return composefile.retag(ref, new_tag) if split_ref(ref)[1] == tag else ref


def _holds(docker: Docker, outcome: Outcome, app: App, info: Container, ref: str, judge: auto.Judge | None = None,
           infos: list[Container] | None = None) -> bool | Verdict:
    """True when the pulled image is mailed, not installed: another major under hold_major, a release whose notes mark a
    breaking change to something this setup uses (breaking_marker), or one whose version can't be read.

    The verdict instead when the morning may install it by itself (auto = "on").
    """
    before, after = (_leading_version(text) for text in (info.version, docker.image_version(ref)))
    if not before or not after:
        outcome.issues.append(Issue(app.key, f"The version of the new {ref} could not be compared with the running one, so it was not installed."))
        return True
    current, target = parse_version(before), parse_version(after)
    releases, marked = None, ()
    if app.hold_major and before.split(".")[0] != after.split(".")[0]:
        what = "is a new major"
    elif not app.breaking_marker or current is None or target is None or not newer(target, current):
        # A rebuild of the version running has no notes of its own.
        return False
    else:
        releases = _releases(app)
        text = "\n".join(release.notes or "" for release in notes.between(releases, current, target))
        if app.breaking_marker not in text:
            return False
        marked = tuple(f"“{line}”" for line in notes.marked(
            text, app.breaking_marker, app.breaking_names, auto.inventory(docker, app, info))[:3])
        if not marked:
            return False
        what = "marks a breaking change"
    url = release_url(app, after)
    if not app.auto or judge is None or current is None or target is None:
        outcome.notices.append(Notice(app.key, before, after, url, True, "manual" if marked else "", marked, held=HELD_BREAKING if marked else ""))
        log(f"{app.title} {before} -> {after} {what}, installed by hand (email)")
        return True
    try:
        releases = releases if releases is not None else _releases(app)
    except Exception as exc:
        log(f"could not read {app.title} releases, so {after} is installed by hand: {exc}")
        outcome.notices.append(Notice(app.key, before, after, url, True))
        return True
    found = next((release for release in releases if release.version == target and not release.prerelease), None) or Release(target, f"{app.tag_prefix}{after}", None)
    infos = infos or [info]
    images = {item.name: item.image.split("@", 1)[0] for item in infos}
    size = 1 if what == "is a new major" else len(target)
    state, verdict = _decide(judge, app, infos, current, found, releases, images, after, size, frozenset())
    if state == "auto" and verdict is not None:
        return verdict
    if marked and state in ("manual", "plain", "unanswered"):
        state = "unanswered" if state == "unanswered" else "manual"
        verdict = Verdict("manual", (*marked, *(verdict.reasons if verdict else ()))[:5], verdict.actions if verdict else (),
                          held=HELD_AI if verdict and verdict.held == HELD_AI else HELD_BREAKING)
    outcome.notices.append(_judged_notice(app, before, after, url, state, verdict, what))
    return True


def _held_semver(app: App, infos: list[Container], current: str, held: Release, releases: list[Release], notices: list[Notice],
                 judge: auto.Judge | None = None, morning: Morning | None = None) -> None:
    """A semver release that waits: a minor its first week, then by itself; a major, or a minor under pin_minor, for a hand or Workers AI."""
    running = parse_version(current)
    if not app.auto or judge is None or running is None or not _by_hand(app, running, held):
        _waiting_notice(app, current, held, notices)
        return
    size = 1 if held.version[0] != running[0] else 2
    images = {}
    for info in infos:
        ref = info.image.split("@", 1)[0]
        tag = split_ref(ref)[1]
        line = tag_line(tag)
        # A tag that already covers the release (`latest`, `v1` for 1.1) stays; `v1.0` becomes `v1.1`.
        new_tag = tag if line is None or held.version[:len(line[0])] == line[0] else composefile.next_tag(tag, held.version)
        images[info.name] = composefile.retag(ref, new_tag) if new_tag else ""
    running_version, available = current.lstrip("v"), held.tag.lstrip("v")
    url = _release_page(app, held.tag)
    if not all(images.values()):
        _waiting_notice(app, current, held, notices)
        return
    rejected = morning.rejected if morning else frozenset()
    state, verdict = _decide(judge, app, infos, running, held, releases, images, available, size, rejected)
    if state == "auto" and morning is not None and not morning.dry:
        version = app.tag_prefix + held.tag.removeprefix("v")

        def fetch() -> dict[str, str]:
            pulled = {}
            for name, ref in images.items():
                exact = f"{split_ref(ref)[0]}:{version}"
                log(f"pulling {exact}")
                morning.docker.pull(exact)
                morning.docker.tag(exact, ref)
                pulled[name] = morning.docker.image_id(ref)
            return pulled

        _move(morning, judge, app, infos, infos[0].stack, images, fetch, running_version, available, url, verdict, [(app.key, available)])
        return
    what = "is a new major" if size == 1 else "is a new minor"
    notices.append(_judged_notice(app, running_version, available, url, state, verdict, what))


def _decide(judge: auto.Judge, app: App, infos: list[Container], current: tuple[int, ...], found: Release, releases: list[Release],
            images: dict[str, str], available: str, size: int, rejected) -> tuple[str, Verdict | None]:
    """"wait" for the line's first wait_days; then "manual", "shadow" or "auto" with the verdict. With no answer, "pending" for
    the first mornings, then "unanswered": by hand, and asked again each morning. When Workers AI can't be asked (no tokens),
    "unanswered" at once, saying so.

    With wait_days 0 it is asked the first morning the release is seen.
    """
    if (app.key, available) in rejected:
        return "manual", Verdict("manual", (f"{available} went in by itself before, and was rolled back because it did not become healthy.",))
    if app.critical and found.version[:1] != current[:1]:
        return "plain", None
    if app.wait_days:
        since = auto.line_since(releases, current, found.version, size)
        if since is None or datetime.now(timezone.utc) - since < timedelta(days=app.wait_days):
            return "wait", None
    problem = _cannot_move(judge.docker, app, infos, images)
    if problem:
        return "manual", Verdict("manual", (f"It can't go in by itself: {problem}.",))
    verdict = judge.verdict(app, infos[0], infos[0].stack, current, found, releases, images, available)
    if verdict is None and judge.unavailable:
        return "unanswered", Verdict("manual", (judge.unavailable,))
    if verdict is None:
        return ("pending" if judge.waiting(app.key, available) else "unanswered"), None
    if not verdict.auto:
        return "manual", verdict
    return ("auto" if app.auto == "on" else "shadow"), verdict


def _cannot_move(docker: Docker, app: App, infos: list[Container], images: dict[str, str]) -> str:
    """Why a tag change can't be made here, before anyone is asked. Empty when it can. Not kept: checked again each morning."""
    stacks = {info.stack for info in infos}
    if len(stacks) != 1 or None in stacks or not all(info.service for info in infos):
        return f"{' and '.join(app.containers)} are not one Compose project"
    stack = infos[0].stack
    changes = [(info.service, info.image.split("@", 1)[0], images[info.name]) for info in infos if images[info.name] != info.image.split("@", 1)[0]]
    try:
        edits = composefile.plan(stack.files, changes)
    except (ValueError, OSError) as exc:
        return str(exc)
    locked = [Path(edit.path).name for edit in edits if not os.access(edit.path, os.W_OK)]
    if locked:
        return f"the updater can't write {', '.join(locked)}"
    if not edits:
        return ""
    try:
        dirty = auto.uncommitted(docker, [edit.path for edit in edits])
    except (DockerError, ValueError) as exc:
        return f"git could not tell whether its compose file is committed: {exc}"
    if dirty:
        return f"{', '.join(dirty)} has changes that are not committed, and the commit would take them too"
    return ""


def _judged_notice(app: App, running: str, available: str, url: str, state: str, verdict: Verdict | None, what: str) -> Notice:
    if state == "wait":
        log(f"{app.title} {running} -> {available} {what}, Workers AI is asked {app.wait_days} days after its first release")
        return Notice(app.key, running, available, url, False, "wait")
    if state == "pending":
        log(f"{app.title} {running} -> {available} {what}, Workers AI is asked again the next morning")
        return Notice(app.key, running, available, url, False, "pending")
    if state == "unanswered":
        log(f"{app.title} {running} -> {available} {what}, installed by hand (email) until Workers AI answers")
        return Notice(app.key, running, available, url, True, "manual" if verdict else "", verdict.reasons if verdict else (), unanswered=True,
                      held=verdict.held if verdict else "")
    if verdict is None:
        log(f"{app.title} {running} -> {available} {what}, installed by hand (email)")
        return Notice(app.key, running, available, url)
    if state == "auto":
        log(f"{app.title} {running} -> {available} {what}, installs by itself the next morning")
        return Notice(app.key, running, available, url, False, "auto", verdict.reasons)
    said = "would install by itself (shadow mode)" if state == "shadow" else "installed by hand"
    log(f"{app.title} {running} -> {available} {what}, {said} (email)")
    return Notice(app.key, running, available, url, True, state, verdict.reasons, verdict.actions,
                  held=verdict.held if state == "manual" else "")


def _move(morning: Morning, judge: auto.Judge, app: App, infos: list[Container], stack: Stack, images: dict[str, str], fetch,
          before: str, version: str, url: str, verdict: Verdict, candidates: list[tuple[str, str]]) -> bool:
    """Install a release by its tag alone: the compose file's image lines, the images, the rollback copies, then a local commit.

    Anything that fails before the containers are recreated leaves them as they were, and is tried again the next morning.
    """
    docker, outcome = morning.docker, morning.outcome
    label = f"{app.title} {version}"
    services = [info.service for info in infos]
    refs = {info.name: info.image.split("@", 1)[0] for info in infos}
    runner = morning.config.compose_container in app.containers
    changed = {info.service: images[info.name] for info in infos if images[info.name] != refs[info.name]}
    edits: list[composefile.Edit] = []

    def back(note: str) -> None:
        try:
            composefile.revert(edits)
            for info in infos:
                docker.tag(info.image_id, refs[info.name])
        except (DockerError, OSError) as exc:
            note = f"{note} Putting the compose file and tags back failed too: {exc}"
        outcome.issues.append(Issue(app.key, f"{version} did not go in by itself: {note} Nothing was changed; it is tried again the next morning."))

    log(f"{app.key} {before} -> {version}, by itself")
    if changed:
        try:
            edits = composefile.plan(stack.files, [(info.service, refs[info.name], images[info.name]) for info in infos if info.service in changed])
            resolved = docker.compose_config(stack)
            composefile.write(edits)
            if not composefile.only_images(resolved, docker.compose_config(stack), changed):
                raise composefile.EditError("the edit changed more than the image")
        except (ValueError, OSError, DockerError) as exc:
            back(_sentence(f"its compose file could not be changed: {exc}"))
            return False
    try:
        new_ids = fetch()
    except DockerError as exc:
        back(_sentence(f"the pull failed: {exc}"))
        return False
    saved = None

    def recreate() -> None:
        if runner:
            docker.recreate_runner(images[app.main], stack)
        else:
            docker.compose(stack, "up", "-d", "--pull", "never", "--no-deps", *services)

    def healthy() -> bool:
        return all(docker.wait_ready(name) for name in app.containers) and all(_runs(docker, name, image) for name, image in new_ids.items())

    def restore() -> bool:
        try:
            composefile.revert(edits)
            for info in infos:
                docker.tag(info.image_id, refs[info.name])
            if saved:
                docker.compose(stack, "up", "--no-start", "--pull", "never", "--no-deps", *services)
                auto.put_back(docker, saved)
                docker.compose(stack, "start", *services)
            elif runner:
                docker.recreate_runner(refs[app.main], stack)
            else:
                docker.compose(stack, "up", "-d", "--pull", "never", "--no-deps", *services)
        except (DockerError, OSError) as exc:
            log(f"{app.key} restore failed: {exc}")
            return False
        return all(docker.wait_ready(info.name) and _runs(docker, info.name, info.image_id) for info in infos)

    try:
        if app.rollback_files:
            try:
                # A rollback clears the saved paths with it, and must not wait on Docker Hub halfway.
                docker.ensure_image(auto.TOOLS)
            except DockerError as exc:
                back(_sentence(f"the image a rollback needs could not be pulled: {exc}"))
                return False
            try:
                docker.compose(stack, "stop", infos[0].service)
                saved = auto.save(docker, app, app.main)
            except (DockerError, OSError) as exc:
                back(_sentence(f"{app.main} could not be backed up first: {exc}"))
                try:
                    docker.compose(stack, "start", infos[0].service)
                except DockerError:
                    pass
                return False
        if not _apply(outcome, app.key, candidates, recreate, restore, label, healthy):
            judge.overrule(app.key, version, f"{version} went in by itself, and was rolled back because it did not become healthy.")
            return False
    except SystemExit:
        # Stopped midway (a shutdown): leave the previous version running, not a stopped app and an edited file.
        log(f"{app.key}: stopped while {version} went in, putting {before} back")
        restore()
        raise
    log(f"{app.key} updated to {version}, by itself")
    reason = " ".join(verdict.reasons)
    if edits:
        error = auto.commit(docker, edits, f"{app.key}: {_tags(changed)}", f"{label} went in by itself. {reason}".strip())
        if error:
            outcome.issues.append(Issue(app.key, f"{version} went in by itself, but the compose file change was not committed: {error}"))
    outcome.bumps.append(Bump(app.key, before, version, url, reason))
    return True


def _tags(changed: dict[str, str]) -> str:
    return ", ".join(f"{service} to {split_ref(ref)[1]}" for service, ref in changed.items())


def _leading_version(label: str) -> str:
    """The version a label starts with: 6.4.4.10685 from linuxserver's 6.4.4.10685-ls325."""
    match = LEADING_VERSION.match(label.strip())
    return match.group(1) if match else ""


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


def _releases(app: App, suffix: str = ""):
    kind, _, repository = app.releases.partition(":")
    return github_releases(repository) if kind == "github" else hub_tags(repository, suffix)


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
    by_hand = True
    if parse_version(current) and held.version[0] != parse_version(current)[0]:
        note = f"{held.tag} is a new major, installed by hand"
    elif app.pin_minor:
        note = f"{held.tag} is a new minor, installed by hand"
    else:
        # Without a publish date the minor is never eligible, so it waits for a hand.
        note, by_hand = wait_note(held, timedelta(days=app.wait_days)), held.published is None
    notices.append(Notice(app.key, running_version, available, _release_page(app, held.tag), by_hand))
    log(f"{app.key} {running_version} -> {available}, {note}")


def _by_hand(app: App, running: tuple[int, ...], held: Release) -> bool:
    """A new major, or a new minor under pin_minor: semver leaves these to a hand. A plain minor installs itself after wait_days."""
    return held.version[0] != running[0] or app.pin_minor


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
