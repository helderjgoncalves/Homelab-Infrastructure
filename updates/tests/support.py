"""Fakes, fixtures and base cases the test modules share. No Docker."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import unittest
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from updater import auto
from updater import composefile
from updater import config
from updater import mail
from updater import main as update
from updater import verdict
from updater.verdict import Verdict
from updater.jobs import Outcome, update_floating
from updater.releases import Release, parse_version
from updater.runtime import Container, DockerError, Stack

NOW = datetime(2026, 11, 12, tzinfo=timezone.utc)
WEEK = timedelta(days=7)
SUNDAY = date(2026, 11, 15)
CONFIG = config.load()
APPS = CONFIG.apps
mail.use_titles(CONFIG.titles)


def release(tag: str, days_ago: int, prerelease: bool = False, now: datetime = NOW) -> Release:
    version = parse_version(tag)
    assert version is not None
    return Release(version, tag, now - timedelta(days=days_ago), prerelease=prerelease)


@dataclass
class Box:
    image: str
    image_id: str
    project: str
    service: str
    # Compose files on the container's labels; one docker-compose.yml when empty.
    files: tuple[str, ...] = ()
    labeled: bool = True


class FakeDocker:
    """Local tags, a registry, and containers that follow compose."""

    def __init__(self, boxes: dict[str, Box], remote: dict[str, str] | None = None, versions: dict[str, str] | None = None):
        self.boxes = boxes
        self.images = {box.image: box.image_id for box in boxes.values()}
        self.remote = remote or {}
        self.versions = versions or {}
        self.unhealthy: set[str] = set()
        self.fail_up = 0
        self.frozen = False
        self.calls: list[tuple] = []
        self.stacks: list[Stack] = []
        self.pruned = 0
        self.prune_error: str | None = None
        self.files: dict[tuple[str, str], bytes] = {}
        self.outputs: dict[tuple[str, str], str] = {}
        self.archives: dict[tuple[str, str], bytes] = {}
        self.compose_container = ""

    def archive(self, name: str, path: str) -> bytes:
        self.calls.append(("archive", name, path))
        if (name, path) not in self.archives:
            raise DockerError(f"{path} not found in {name}")
        return self.archives[(name, path)]

    def put_archive(self, name: str, path: str, data: bytes) -> None:
        self.calls.append(("put_archive", name, path, data))
        self.archives[(name, path)] = data

    def exec(self, name: str, cmd: list[str]) -> tuple[int, str]:
        output = self.outputs.get((name, self.boxes[name].image_id))
        return (0, output) if output is not None else (127, "not found")

    def read_file(self, name: str, path: str) -> bytes:
        key = (name, self.boxes[name].image_id)
        if key not in self.files:
            raise DockerError(f"{path} not found in {name}")
        return self.files[key]

    def names(self) -> set[str]:
        return set(self.boxes)

    def inspect(self, name: str) -> Container:
        if name not in self.boxes:
            raise DockerError(f"no such container {name}")
        box = self.boxes[name]
        version = self.versions.get(box.image_id, box.image.rsplit(":", 1)[-1])
        if not box.labeled:
            return Container(name, box.image, box.image_id, version, "running", "healthy")
        stack = Stack(box.project, box.files or (f"/stacks/{box.project}/docker-compose.yml",), f"/stacks/{box.project}")
        return Container(name, box.image, box.image_id, version, "running", "healthy", 0, box.service, stack)

    def image_id(self, ref: str) -> str:
        if ref not in self.images:
            raise DockerError(f"no such image {ref}")
        return self.images[ref]

    def image_version(self, ref: str) -> str:
        return self.versions.get(self.image_id(ref), "")

    def pull(self, ref: str) -> None:
        self.calls.append(("pull", ref))
        if ref not in self.remote:
            raise DockerError(f"manifest unknown {ref}")
        self.images[ref] = self.remote[ref]

    def tag(self, source: str, target: str) -> None:
        self.calls.append(("tag", source, target))
        self.images[target] = self.images.get(source, source)

    def compose(self, stack: Stack, *args: str) -> None:
        self.calls.append(("compose", stack.project, *args))
        self.stacks.append(stack)
        services = [arg for arg in args[1:] if not arg.startswith("-") and arg != "never"]
        boxes = [box for box in self.boxes.values() if box.project == stack.project and box.service in services]
        if args[0] == "pull":
            for box in boxes:
                if box.image in self.remote:
                    self.images[box.image] = self.remote[box.image]
        elif args[0] == "up":
            if self.fail_up:
                self.fail_up -= 1
                raise DockerError("up failed")
            if not self.frozen:
                for box in boxes:
                    box.image_id = self.images[box.image]

    def recreate_runner(self, image: str, stack: Stack) -> None:
        self.calls.append(("recreate", stack.project))
        services = [box.service for box in self.boxes.values() if box.project == stack.project]
        self.compose(stack, "up", "-d", *services)

    def wait_ready(self, name: str) -> bool:
        return self.boxes[name].image_id not in self.unhealthy

    def prune_unused(self) -> tuple[int, int]:
        if self.prune_error:
            raise DockerError(self.prune_error)
        self.pruned += 1
        return 3, 1048576


def traefik(remote: str = "new", image: str = "traefik:v3") -> FakeDocker:
    return FakeDocker({"traefik": Box(image, "old", "traefik", "traefik")}, {image: remote})


def bump(docker: FakeDocker, key: str, outcome: Outcome, rejected=frozenset(), running=None) -> None:
    update_floating(docker, CONFIG, set(docker.boxes) if running is None else running, False, outcome, rejected, APPS[key])


def bump_traefik(docker: FakeDocker, outcome: Outcome, rejected=frozenset()) -> None:
    bump(docker, "traefik", outcome, rejected)



class StateTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        state = self.dir / "state"
        paths = {
            "STATE": state,
            "NOTIFIED": state / "notified.tsv",
            "PUSHED": state / "pushed.tsv",
            "FAILURES": state / "failures.tsv",
            "BUMPS": state / "weekly-bumps.tsv",
            "DUE": state / "digest.due",
            "REJECTED": state / "rejected.tsv",
            "HOLD": state / "prune.hold",
            "PRUNE_ERROR": state / "prune.error",
            "UNCHECKED": state / "unchecked.tsv",
            "VERDICTS": state / "verdicts.tsv",
            "ASKING": state / "asking.tsv",
            "JOURNAL": state / "workers-ai",
            "LOCK": state / "lock",
        }
        for name, value in paths.items():
            patcher = mock.patch.object(update, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.letters = []
        self.send_ok = True

        def deliver(letter, dry):
            self.letters.append(letter)
            return 0 if self.send_ok else 1

        patcher = mock.patch.object(update, "_deliver", deliver)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.pushes = []
        self.push_error: Exception | None = None

        def send_push(bump, topic, server):
            if self.push_error:
                raise self.push_error
            self.pushes.append((bump, topic, server))

        for target, name, value in ((update, "ENV_FILE", self.dir / ".env"), (update.push, "send", send_push)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def report(self, day: date, notices=(), bumps=(), issues=(), weekly=False) -> int:
        env = {"UPDATE_TODAY": day.isoformat(), "UPDATE_WDAY": str(day.isoweekday())}
        with mock.patch.dict(os.environ, env):
            return update._send_report(list(notices), list(bumps), list(issues), False, weekly=weekly)

    def subjects(self) -> list[str]:
        subjects = [letter.subject for letter in self.letters]
        self.letters.clear()
        return subjects



class MainCase(StateTests):
    """update.main with a fake Docker and run(); the tests set self.outcome."""

    def setUp(self):
        super().setUp()
        self.docker = FakeDocker({})
        self.outcome = Outcome()
        self.run = mock.Mock(side_effect=lambda *_args: self.outcome)
        for name, value in (
            ("Docker", lambda: self.docker),
            ("run", self.run),
            ("collect_notices", lambda *_args, **_kwargs: None),
        ):
            patcher = mock.patch.object(update, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(update.signal, "signal")
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.dict(os.environ, {"UPDATE_TODAY": "2026-11-15", "UPDATE_WDAY": "7"})
        patcher.start()
        self.addCleanup(patcher.stop)



IMAGE_LINE = re.compile(r"^[ \t]*image:[ \t]*[\"']?([^\s\"'#]+)", re.M)


class FileDocker(FakeDocker):
    """Compose reads each service's image from its compose file, as on the NAS. Rollback copies are real files."""

    def __init__(self, boxes: dict[str, Box], remote: dict[str, str] | None = None, versions: dict[str, str] | None = None):
        super().__init__(boxes, remote, versions)
        self.ran: list[tuple] = []
        # What `git status --porcelain` prints: "" when the compose files are as committed.
        self.status = ""
        self.statuses: list[list[str]] = []
        self.run_code = 0
        self.paths: dict[tuple[str, str], bytes] = {}
        self.configs = 0
        self.config_hook = None
        self.unpullable: set[str] = set()

    def _services(self, stack: Stack) -> dict[str, dict]:
        services = {}
        for path in stack.files:
            text = Path(path).read_text()
            for box in self.boxes.values():
                found = composefile.block(text, box.service) if box.project == stack.project else ""
                match = IMAGE_LINE.search(found)
                if match:
                    services[box.service] = {"image": match.group(1), "rest": IMAGE_LINE.sub("", found)}
        return services

    def compose(self, stack: Stack, *args: str) -> None:
        for service, resolved in self._services(stack).items():
            for box in self.boxes.values():
                if box.project == stack.project and box.service == service:
                    box.image = resolved["image"]
        super().compose(stack, *args)

    def compose_config(self, stack: Stack) -> dict:
        self.calls.append(("config", stack.project))
        self.configs += 1
        resolved = {"services": self._services(stack)}
        return self.config_hook(self.configs, resolved) if self.config_hook else resolved

    def read_file(self, name: str, path: str) -> bytes:
        if (name, path) in self.paths:
            return self.paths[(name, path)]
        return super().read_file(name, path)

    def archive_to(self, name: str, path: str, target: Path) -> bool:
        self.calls.append(("archive_to", name, path))
        if (name, path) not in self.archives:
            return False
        target.write_bytes(self.archives[(name, path)])
        return True

    def put_archive_file(self, name: str, path: str, source: Path) -> None:
        self.calls.append(("put_archive_file", name, path))
        self.archives[(name, path)] = source.read_bytes()

    def ensure_image(self, image: str) -> None:
        self.calls.append(("ensure_image", image))
        if image in self.unpullable:
            raise DockerError(f"pull of {image} failed")

    def run_once(self, image: str, cmd: list[str], **options) -> tuple[int, str]:
        if "status" in cmd:
            self.statuses.append(cmd)
            return 0, self.status
        self.ran.append((image, cmd, options))
        if options.get("entrypoint") == ["rm"]:
            for container in options.get("volumes_from") or []:
                for path in cmd[2:]:
                    self.archives.pop((container, path), None)
        return self.run_code, "" if self.run_code == 0 else "fatal: boom"


def noted(tag: str, days_ago: int, text: str = "") -> Release:
    return replace(release(tag, days_ago, now=datetime.now(timezone.utc)), notes=text)


class AutoCase(unittest.TestCase):
    """Workers AI answers what self.answer says; the stacks checkout is a temporary folder."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.asked: list[verdict.Question] = []
        self.answer: Verdict | Exception = Verdict("auto", ("Only fixes.",))

        def ask(question, env):
            self.asked.append(question)
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer

        for target, name, value in ((auto, "STACKS", self.dir), (auto, "ROLLBACK", self.dir / "rollback"), (auto, "ask", ask)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def compose_file(self, project: str, text: str) -> Path:
        path = self.dir / project / "docker-compose.yml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def judge(self, docker, known=None, asking: bool = True) -> auto.Judge:
        return auto.Judge(docker, {}, known or {}, asking, journal=self.dir / "journal")

    def journal(self) -> list[dict]:
        return [json.loads(line) for path in sorted((self.dir / "journal").glob("*.jsonl")) for line in path.read_text().splitlines()]


TRAEFIK_COMPOSE = """services:
  traefik:
    # A new major is mailed, moved to by hand.
    image: "traefik:v3"   # the major it follows
    command:
      - --api.dashboard=true
    environment:
      TZ: Europe/Lisbon
"""
TRAEFIK_URL = "https://github.com/traefik/traefik/releases/tag/v4.0.1"
