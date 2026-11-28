"""Rules, state and mail, without Docker."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import stat
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import auto
import composefile
import config
import jobs
import mail
import notes
import push
import runtime
import update
import verdict
from verdict import HELD_AI, HELD_BREAKING, Verdict, VerdictError
from config import ConfigError
from jobs import HOLD_NOTE, Outcome, apply_update, run, update_floating, update_semver
from mail import Bump, Issue, Notice, failure_letter, pinned_letter
from releases import Firmware, Release, choose, newer, parse_version, qts_firmware
from runtime import Container, DockerError, Stack, api_prefix, stack_of, unused_image_prune_path
from update import Kept, digest_due, merge_bumps

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


class ChooseTests(unittest.TestCase):
    def test_patch_is_taken_immediately(self):
        tag, waiting = choose("v3.4.1", [release("v3.4.2", 1)], NOW)
        self.assertEqual(tag, "v3.4.2")
        self.assertIsNone(waiting)

    def test_new_minor_waits_a_week(self):
        tag, waiting = choose("v3.4.1", [release("v3.5.0", 6)], NOW, wait=WEEK)
        self.assertIsNone(tag)
        self.assertEqual(waiting.tag, "v3.5.0")

    def test_new_minor_is_taken_after_a_week(self):
        tag, waiting = choose("3.4.1", [release("v3.5.0", 7)], NOW, wait=WEEK)
        self.assertEqual(tag, "v3.5.0")
        self.assertIsNone(waiting)

    def test_with_no_wait_a_new_minor_is_taken_that_morning(self):
        undated = Release((3, 5, 0), "v3.5.0", None)
        self.assertEqual(choose("v3.4.1", [release("v3.5.0", 0)], NOW), ("v3.5.0", None))
        self.assertEqual(choose("v3.4.1", [undated], NOW), ("v3.5.0", None))

    def test_patch_of_a_new_minor_does_not_wait(self):
        tag, _waiting = choose("v3.4.1", [release("v3.5.0", 1), release("v3.5.1", 0)], NOW, wait=WEEK)
        self.assertEqual(tag, "v3.5.1")

    def test_a_newer_major_is_held_and_prereleases_are_ignored(self):
        tag, waiting = choose("v3.7.18", [release("v4.0.0", 30), release("v4.1.0", 2), release("v3.8.0", 1, prerelease=True)], NOW)
        self.assertIsNone(tag)
        self.assertEqual(waiting.tag, "v4.1.0")
        self.assertEqual(choose("v3.7.18", [release("v2.9.0", 1)], NOW), (None, None))

    def test_a_waiting_minor_is_named_before_a_new_major(self):
        tag, waiting = choose("v3.4.1", [release("v3.4.2", 0), release("v3.5.0", 1), release("v4.0.0", 0)], NOW, wait=WEEK)
        self.assertEqual((tag, waiting.tag), ("v3.4.2", "v3.5.0"))
        tag, waiting = choose("v3.4.1", [release("v3.5.0", 8), release("v4.0.0", 0)], NOW, wait=WEEK)
        self.assertEqual((tag, waiting.tag), ("v3.5.0", "v4.0.0"))

    def test_a_pinned_minor_takes_patches_and_holds_back_any_newer_minor(self):
        releases = [release("v1.0.1", 0), release("v1.1.0", 30), release("v1.1.2", 9), release("v2.0.0", 0), release("v1.2.0", 0, prerelease=True)]
        tag, waiting = choose("v1.0.0", releases, NOW, pin_minor=True)
        self.assertEqual((tag, waiting.tag), ("v1.0.1", "v1.1.2"))
        self.assertEqual(choose("v1.0.1", releases[1:3], NOW, pin_minor=True)[0], None)


class RollbackTests(unittest.TestCase):
    def test_apply_update_restores_when_unhealthy(self):
        restored = []
        detail, back = apply_update(lambda: None, lambda: restored.append(True) or True, "The new image", lambda: False)
        self.assertEqual(restored, [True])
        self.assertTrue(back)
        self.assertIn("previous image was restored", detail)

    def test_apply_update_reports_a_failed_restore(self):
        def recreate():
            raise DockerError("boom")

        detail, back = apply_update(recreate, lambda: False, "The new image", lambda: True)
        self.assertFalse(back)
        self.assertIn("Restoring the previous image failed", detail)
        self.assertIn("boom", detail)

    def test_unhealthy_image_is_rolled_back_and_not_tried_again(self):
        docker = traefik()
        docker.unhealthy = {"new"}
        first = Outcome()
        bump_traefik(docker, first)
        self.assertEqual(docker.boxes["traefik"].image_id, "old")
        self.assertEqual(docker.images["traefik:v3"], "old")
        self.assertEqual(first.rejected, [("traefik", "new")])
        self.assertEqual(first.hold, [])
        self.assertEqual(first.bumps, [])
        self.assertIn("previous image was restored", first.issues[0].detail)

        docker.calls.clear()
        second = Outcome()
        bump_traefik(docker, second, set(first.rejected))
        self.assertFalse([call for call in docker.calls if call[:3] == ("compose", "traefik", "up")])
        self.assertEqual(docker.images["traefik:v3"], "old")
        self.assertEqual(second.issues, [])

    def test_a_newer_image_after_a_rollback_is_tried(self):
        docker = traefik(remote="newer")
        outcome = Outcome()
        bump_traefik(docker, outcome, {("traefik", "new")})
        self.assertEqual(docker.boxes["traefik"].image_id, "newer")
        self.assertEqual([bump.key for bump in outcome.bumps], ["traefik"])

    def test_failed_restore_holds_the_prune(self):
        docker = traefik()
        docker.fail_up = 2
        outcome = Outcome()
        bump_traefik(docker, outcome)
        self.assertEqual(outcome.rejected, [])
        self.assertEqual(len(outcome.hold), 1)
        self.assertIn("Restoring the previous image failed", outcome.issues[0].detail)
        self.assertIn(HOLD_NOTE, outcome.issues[0].detail)

    def test_image_keeps_an_app_on_its_tag(self):
        docker = traefik(image="traefik:v4")
        outcome = Outcome()
        update_floating(docker, CONFIG, {"traefik"}, False, outcome, frozenset(), replace(APPS["traefik"], image="traefik:v3"))
        self.assertEqual(docker.calls, [])
        self.assertIn("not traefik:v3", outcome.issues[0].detail)

    def test_unlabeled_apps_report_their_package_version(self):
        for name, before, after, url in (
            ("uptime-kuma", "2.5.9", "2.5.11", "https://github.com/louislam/uptime-kuma/releases/tag/2.5.11"),
            ("dockge", "1.4.2", "1.5.0", "https://github.com/louislam/dockge/releases/tag/1.5.0"),
        ):
            image = f"louislam/{name}:2"
            docker = FakeDocker({name: Box(image, "old", name, name)}, {image: "new"})
            docker.files = {
                (name, "old"): ('{"name": "%s", "version": "%s"}' % (name, before)).encode(),
                (name, "new"): ('{"name": "%s", "version": "%s"}' % (name, after)).encode(),
            }
            outcome = Outcome()
            bump(docker, name, outcome)
            self.assertEqual(outcome.bumps, [Bump(name, before, after, url)])
            self.assertIn(f"{before} → {after}", pinned_letter([], outcome.bumps).text)

    def test_cloudflared_reports_the_version_of_its_binary(self):
        image = "cloudflare/cloudflared:latest"
        docker = FakeDocker({"cloudflare-tunnel": Box(image, "old", "cloudflared", "cloudflare-tunnel")}, {image: "new"})
        docker.outputs = {
            ("cloudflare-tunnel", "old"): "cloudflared version 2026.10.7 (built 2026-10-30-17:17 UTC)\n",
            ("cloudflare-tunnel", "new"): "cloudflared version 2026.11.0 (built 2026-11-12-09:02 UTC)\n",
        }
        outcome = Outcome()
        bump(docker, "cloudflare-tunnel", outcome)
        self.assertEqual(outcome.bumps, [Bump("cloudflare-tunnel", "2026.10.7", "2026.11.0", "https://github.com/cloudflare/cloudflared/releases/tag/2026.11.0")])

    def test_an_unreadable_package_falls_back_to_the_tag(self):
        docker = FakeDocker({"uptime-kuma": Box("louislam/uptime-kuma:2", "old", "uptime-kuma", "uptime-kuma")}, {"louislam/uptime-kuma:2": "new"})
        docker.files = {("uptime-kuma", "new"): b"not json"}
        outcome = Outcome()
        bump(docker, "uptime-kuma", outcome)
        self.assertEqual((outcome.bumps[0].before, outcome.bumps[0].after), ("2", "2"))

    def test_read_file_unpacks_the_archive(self):
        import io
        import tarfile
        from runtime import Docker

        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            content = b'{"version": "2.5.9"}'
            member = tarfile.TarInfo("package.json")
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
        docker = Docker()
        with mock.patch.object(docker, "_read", return_value=buffer.getvalue()) as read:
            self.assertEqual(docker.read_file("uptime-kuma", "/app/package.json"), content)
        self.assertIn("/containers/uptime-kuma/archive?path=%2Fapp%2Fpackage.json", read.call_args.args[0])
        with mock.patch.object(docker, "_read", return_value=b"garbage"):
            with self.assertRaises(DockerError):
                docker.read_file("uptime-kuma", "/app/package.json")

    def test_put_archive_sends_the_tar_to_the_files_directory(self):
        from runtime import Docker

        docker = Docker()
        with mock.patch.object(docker, "_open", return_value=(200, b"")) as opened:
            docker.put_archive("adguard", "/opt/adguardhome/conf/AdGuardHome.yaml", b"tar")
        method, path, body = opened.call_args.args
        self.assertEqual((method, path, body), ("PUT", "/containers/adguard/archive?path=%2Fopt%2Fadguardhome%2Fconf", b"tar"))
        self.assertEqual(opened.call_args.kwargs, {"content_type": "application/x-tar"})
        with mock.patch.object(docker, "_open", return_value=(404, b'{"message":"no such container"}')):
            with self.assertRaisesRegex(DockerError, "no such container"):
                docker.put_archive("adguard", "/opt/adguardhome/conf/AdGuardHome.yaml", b"tar")

    def wait(self, states: list[tuple[str, str, int]]) -> bool:
        from runtime import Docker

        docker = Docker()
        sequence = iter(Container("beszel", "henrygd/beszel:0", "id", "0", status, health, restarts) for status, health, restarts in states)
        with mock.patch.object(docker, "inspect", side_effect=lambda _name: next(sequence)), mock.patch("runtime.time.sleep"):
            return docker.wait_ready("beszel", attempts=len(states))

    def test_no_healthcheck_must_stay_up_for_three_checks(self):
        self.assertTrue(self.wait([("running", "none", 0)] * 3))
        self.assertFalse(self.wait([("running", "none", 0), ("running", "none", 0), ("restarting", "none", 1)]))

    def test_a_restart_while_waiting_fails(self):
        self.assertFalse(self.wait([("running", "starting", 0), ("running", "starting", 1), ("running", "healthy", 1)]))
        self.assertTrue(self.wait([("running", "starting", 4), ("running", "healthy", 4)]))

    def test_beszel_needs_hub_and_agent(self):
        docker = FakeDocker({"beszel": Box("henrygd/beszel:0", "old", "beszel", "beszel")}, {"henrygd/beszel:0": "new"})
        outcome = Outcome()
        bump(docker, "beszel", outcome, running={"beszel"})
        self.assertEqual(docker.calls, [])
        self.assertEqual(outcome.issues, [])


class ImmichTests(unittest.TestCase):
    SERVER = "ghcr.io/immich-app/immich-server"
    LEARNING = "ghcr.io/immich-app/immich-machine-learning"

    def setUp(self):
        self.docker = FakeDocker(
            {
                "immich_server": Box(f"{self.SERVER}:v3", "srv-old", "immich", "immich-server"),
                "immich_machine_learning": Box(f"{self.LEARNING}:v3", "ml-old", "immich", "immich-machine-learning"),
            },
            {f"{self.SERVER}:v3.4.2": "srv-new", f"{self.LEARNING}:v3.4.2": "ml-new"},
            {"srv-old": "v3.4.1", "ml-old": "v3.4.1", "srv-new": "v3.4.2", "ml-new": "v3.4.2"},
        )
        now = datetime.now(timezone.utc)
        patcher = mock.patch.object(jobs, "github_releases", return_value=[release("v3.4.2", 0, now=now)])
        patcher.start()
        self.addCleanup(patcher.stop)

    def update(self, rejected=frozenset(), running=None) -> Outcome:
        outcome = Outcome()
        update_semver(self.docker, CONFIG, running or set(self.docker.boxes), False, outcome, rejected, APPS["immich"])
        return outcome

    def test_patch_pulls_the_exact_tag_and_retags_v3(self):
        outcome = self.update()
        pulls = [call[1] for call in self.docker.calls if call[0] == "pull"]
        self.assertEqual(pulls, [f"{self.SERVER}:v3.4.2", f"{self.LEARNING}:v3.4.2"])
        self.assertEqual(self.docker.images[f"{self.SERVER}:v3"], "srv-new")
        self.assertIn(("compose", "immich", "up", "-d", "--pull", "never", "--no-deps", "immich-server", "immich-machine-learning"), self.docker.calls)
        self.assertEqual(outcome.bumps, [Bump("immich", "3.4.1", "3.4.2", "https://github.com/immich-app/immich/releases/tag/v3.4.2")])

    def test_unhealthy_immich_is_not_rolled_back(self):
        self.docker.unhealthy = {"srv-new"}
        outcome = self.update()
        self.assertEqual(self.docker.boxes["immich_server"].image_id, "srv-new")
        self.assertEqual(self.docker.images[f"{self.SERVER}:v3"], "srv-new")
        self.assertFalse([call for call in self.docker.calls if call[0] == "tag" and call[1] in {"srv-old", "ml-old"}])
        self.assertIn("not rolled back", outcome.issues[0].detail)
        self.assertEqual(outcome.hold, [])
        self.assertEqual([(bump.before, bump.after) for bump in outcome.bumps], [("3.4.1", "3.4.2")])

    def test_failed_recreate_is_not_retried_for_the_same_version(self):
        self.docker.fail_up = 1
        first = self.update()
        self.assertEqual(first.rejected, [("immich", "v3.4.2")])
        self.assertIn("not rolled back", first.issues[0].detail)
        self.docker.calls.clear()
        self.update(set(first.rejected))
        self.assertEqual(self.docker.calls, [])

    def test_compose_that_ignores_the_pulled_images_is_reported(self):
        self.docker.frozen = True
        outcome = self.update()
        self.assertIn("Compose did not start the v3.4.2 images", outcome.issues[0].detail)
        self.assertIn("(v3)", outcome.issues[0].detail)
        self.assertEqual(outcome.bumps, [])

    def test_failed_pull_changes_nothing(self):
        del self.docker.remote[f"{self.LEARNING}:v3.4.2"]
        outcome = self.update()
        self.assertEqual(self.docker.images[f"{self.SERVER}:v3"], "srv-old")
        self.assertFalse([call for call in self.docker.calls if call[0] == "compose"])
        self.assertFalse([call for call in self.docker.calls if call[0] == "tag" and call[2].endswith(":v3") and call[1].endswith(":v3.4.2")])
        self.assertIn("No container was changed", outcome.issues[0].detail)

    def test_server_alone_is_left_alone(self):
        self.update(running={"immich_server"})
        self.assertEqual(self.docker.calls, [])


class AdGuardTests(unittest.TestCase):
    IMAGE = "adguard/adguardhome"
    CONF = "/opt/adguardhome/conf/AdGuardHome.yaml"

    def setUp(self):
        self.docker = FakeDocker(
            {"adguard": Box(f"{self.IMAGE}:v1.0", "ag-old", "adguard", "adguard")},
            {f"{self.IMAGE}:v1.0.1": "ag-new", f"{self.IMAGE}:v1.1.0": "ag-minor", f"{self.IMAGE}:v2.0.0": "ag-v2"},
            {"ag-old": "v1.0.0", "ag-new": "v1.0.1", "ag-minor": "v1.1.0", "ag-v2": "v2.0.0"},
        )
        self.docker.archives[("adguard", self.CONF)] = b"schema 35"
        now = datetime.now(timezone.utc)
        self.releases = [release("v1.0.1", 0, now=now), release("v1.1.0", 30, now=now), release("v2.0.0", 0, now=now)]

    def update(self, rejected=frozenset()) -> Outcome:
        outcome = Outcome()
        with mock.patch.object(jobs, "github_releases", return_value=self.releases):
            update_semver(self.docker, CONFIG, set(self.docker.boxes), False, outcome, rejected, APPS["adguard"])
        return outcome

    def test_a_patch_is_taken_that_morning_and_tagged_v1_0(self):
        outcome = self.update()
        self.assertEqual([call[1] for call in self.docker.calls if call[0] == "pull"], [f"{self.IMAGE}:v1.0.1"])
        self.assertEqual(self.docker.images[f"{self.IMAGE}:v1.0"], "ag-new")
        self.assertEqual(self.docker.boxes["adguard"].image_id, "ag-new")
        self.assertEqual([(bump.before, bump.after) for bump in outcome.bumps], [("1.0.0", "1.0.1")])
        self.assertEqual(outcome.issues, [])

    def test_a_new_minor_is_only_mailed(self):
        self.releases = self.releases[1:]
        outcome = self.update()
        self.assertEqual(self.docker.calls, [])
        self.assertEqual(outcome.notices, [Notice("adguard", "1.0.0", "1.1.0", "https://github.com/AdguardTeam/AdGuardHome/releases/tag/v1.1.0")])
        self.assertEqual((outcome.bumps, outcome.issues), ([], []))

    def test_a_new_major_alone_is_only_mailed(self):
        self.releases = self.releases[2:]
        outcome = self.update()
        self.assertEqual(self.docker.calls, [])
        self.assertEqual(outcome.notices, [Notice("adguard", "1.0.0", "2.0.0", "https://github.com/AdguardTeam/AdGuardHome/releases/tag/v2.0.0")])
        self.assertEqual((outcome.bumps, outcome.issues), ([], []))

    def test_an_unhealthy_update_is_rolled_back_with_its_config_put_back_first(self):
        self.docker.unhealthy = {"ag-new"}
        self.docker.archives[("adguard", self.CONF)] = b"schema 35"

        def migrate(stack, *args):
            original(stack, *args)
            if args[:2] == ("up", "-d") and self.docker.boxes["adguard"].image_id == "ag-new":
                self.docker.archives[("adguard", self.CONF)] = b"schema 36"

        original, self.docker.compose = self.docker.compose, migrate
        outcome = self.update()
        self.assertEqual(self.docker.boxes["adguard"].image_id, "ag-old")
        self.assertEqual(self.docker.images[f"{self.IMAGE}:v1.0"], "ag-old")
        self.assertEqual(self.docker.archives[("adguard", self.CONF)], b"schema 35")
        steps = [call[:3] for call in self.docker.calls if call[0] in ("compose", "put_archive")][-3:]
        self.assertEqual(steps, [("compose", "adguard", "up"), ("put_archive", "adguard", self.CONF), ("compose", "adguard", "start")])
        self.assertIn("--no-start", self.docker.calls[self.docker.calls.index(("put_archive", "adguard", self.CONF, b"schema 35")) - 1])
        self.assertEqual(outcome.rejected, [("adguard", "v1.0.1")])
        self.assertIn("v1.0.1 did not become healthy. The previous image was restored.", outcome.issues[0].detail)
        self.assertEqual((outcome.bumps, outcome.hold), ([], []))

    def test_a_failed_recreate_is_rolled_back_and_not_tried_again(self):
        self.docker.fail_up = 1
        outcome = self.update()
        self.assertEqual(self.docker.boxes["adguard"].image_id, "ag-old")
        self.assertIn("Recreate failed: up failed. The previous image was restored.", outcome.issues[0].detail)
        self.docker.calls.clear()
        self.update(set(outcome.rejected))
        self.assertEqual(self.docker.calls, [])

    def test_a_failed_restore_keeps_the_previous_image_from_the_prune(self):
        self.docker.unhealthy = {"ag-new", "ag-old"}
        outcome = self.update()
        self.assertIn("Restoring the previous image failed.", outcome.issues[0].detail)
        self.assertIn(HOLD_NOTE, outcome.issues[0].detail)
        self.assertEqual(len(outcome.hold), 1)
        self.assertEqual(outcome.rejected, [])

    def test_no_backup_no_update(self):
        del self.docker.archives[("adguard", self.CONF)]
        outcome = self.update()
        self.assertFalse([call for call in self.docker.calls if call[0] in ("pull", "tag", "compose")])
        self.assertIn("could not be backed up first", outcome.issues[0].detail)

    def test_immich_is_still_not_rolled_back(self):
        self.assertEqual((APPS["immich"].rollback, APPS["immich"].rollback_files), (False, ()))


class PullRetryTests(unittest.TestCase):
    LIMITED = (500, '{"message":"toomanyrequests: retry-after: 66.816µs, allowed: 44000/minute"}'.encode())
    STACK = Stack("immich", ("/stacks/immich/docker-compose.yml",), "/stacks/immich")

    def setUp(self):
        from runtime import Docker

        self.docker = Docker(compose_container="dockge")
        patcher = mock.patch("runtime.time.sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def pull(self, *answers: tuple[int, bytes]):
        with mock.patch.object(self.docker, "_open", side_effect=list(answers)) as opened:
            self.docker.pull("ghcr.io/immich-app/immich-server:v3.4.2")
        return opened

    def test_a_rate_limited_pull_is_tried_again(self):
        opened = self.pull(self.LIMITED, (200, b'{"status":"Downloaded newer image"}'))
        self.assertEqual(opened.call_count, 2)
        self.sleep.assert_called_once_with(30)

    def test_a_rate_limit_in_the_pull_stream_is_tried_again(self):
        stream = b'{"status":"Pulling"}\n{"error":"toomanyrequests: Too Many Requests"}\n'
        opened = self.pull((200, stream), (200, b'{"status":"Pulled"}'))
        self.assertEqual(opened.call_count, 2)

    def test_a_pull_still_rate_limited_after_three_tries_fails(self):
        with self.assertRaisesRegex(DockerError, "toomanyrequests"):
            self.pull(self.LIMITED, self.LIMITED, self.LIMITED)
        self.assertEqual(self.sleep.call_count, 2)

    def test_any_other_pull_error_fails_at_once(self):
        with self.assertRaisesRegex(DockerError, "manifest unknown"):
            self.pull((404, b'{"message":"manifest unknown"}'), (200, b""))
        self.sleep.assert_not_called()

    def test_compose_pull_is_tried_again_and_up_is_not(self):
        limited = (1, "Error response from daemon: toomanyrequests: retry-after: 1s")
        with mock.patch.object(self.docker, "_exec", side_effect=[limited, (0, "")]) as executed:
            self.docker.compose(self.STACK, "pull", "immich-server")
        self.assertEqual(executed.call_count, 2)
        with mock.patch.object(self.docker, "_exec", side_effect=[limited, (0, "")]) as executed:
            with self.assertRaises(DockerError):
                self.docker.compose(self.STACK, "up", "-d", "--pull", "never", "immich-server")
        self.assertEqual(executed.call_count, 1)


class RunTests(unittest.TestCase):
    def setUp(self):
        for patcher in (
            mock.patch.dict(os.environ, {"PLEX_VERSION": "", "QTS_VERSION": ""}),
            mock.patch.object(jobs, "github_releases", return_value=[]),
            mock.patch.object(jobs, "hub_tags", return_value=[]),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_an_error_in_one_step_keeps_the_others(self):
        docker = FakeDocker({
            "traefik": Box("traefik:v3", "old", "traefik", "traefik"),
            "uptime-kuma": Box("louislam/uptime-kuma:2", "kuma", "uptime-kuma", "uptime-kuma"),
        }, {"traefik:v3": "new"})
        original = docker.inspect

        def inspect(name):
            if name == "uptime-kuma":
                raise ValueError("bad payload")
            return original(name)

        docker.inspect = inspect
        outcome = run(docker, CONFIG, False)
        self.assertEqual([bump.key for bump in outcome.bumps], ["traefik"])
        self.assertEqual([issue.name for issue in outcome.issues], ["uptime-kuma"])
        self.assertIn("bad payload", outcome.issues[0].detail)

    def test_unreachable_docker_is_an_issue(self):
        docker = FakeDocker({})

        def names():
            raise DockerError("connection refused")

        docker.names = names
        outcome = run(docker, CONFIG, False)
        self.assertEqual(outcome.issues[0].name, "docker")

    def test_a_failed_release_check_is_recorded_for_the_main_container(self):
        docker = FakeDocker({
            "opencloud-opencloud-1": Box("opencloudeu/opencloud:9.2.0", "oc", "opencloud", "opencloud"),
            "opencloud-collabora-1": Box("collabora/code:25.04.1.1", "co", "opencloud", "collabora"),
            "mosquitto": Box("eclipse-mosquitto:2.1-alpine", "mq", "zigbee2mqtt", "mosquitto"),
        }, versions={"mq": "2.1.4"})
        with mock.patch.object(jobs, "github_releases", side_effect=OSError("rate limited")), \
                mock.patch.object(jobs, "hub_tags", side_effect=OSError("hub down")):
            outcome = run(docker, CONFIG, False)
        self.assertEqual(outcome.unchecked, [("opencloud-opencloud-1", "rate limited"), ("mosquitto", "hub down")])
        self.assertEqual(outcome.issues, [])


class OutsideTests(unittest.TestCase):
    """A floating app's release that its tag will never bring is mailed."""

    def notices(self, docker: FakeDocker, github=(), hub=()) -> list[Notice]:
        notices: list[Notice] = []
        with mock.patch.object(jobs, "github_releases", return_value=list(github)) as self.github, \
                mock.patch.object(jobs, "hub_tags", return_value=list(hub)) as self.hub:
            jobs.collect_outside(docker, CONFIG, set(docker.boxes), notices)
        return notices

    def test_the_tag_line(self):
        self.assertEqual(jobs.tag_line("v3.7"), ((3, 7), ""))
        self.assertEqual(jobs.tag_line("2026.11"), ((2026, 11), ""))
        self.assertEqual(jobs.tag_line("2.1-alpine"), ((2, 1), "-alpine"))
        self.assertEqual(jobs.tag_line("0"), ((0,), ""))
        self.assertIsNone(jobs.tag_line("latest"))
        self.assertIsNone(jobs.tag_line("stable-alpine"))

    def test_a_new_minor_past_a_minor_tag_is_mailed(self):
        docker = FakeDocker({"traefik": Box("traefik:v3.7", "t", "traefik", "traefik")}, versions={"t": "v3.7.19"})
        notices = self.notices(docker, [release("v3.7.20", 0), release("v3.8.1", 3), release("v4.0.0", 0, prerelease=True)])
        self.assertEqual(notices, [Notice("traefik", "3.7.19", "3.8.1", "https://github.com/traefik/traefik/releases/tag/v3.8.1")])

    def test_a_patch_on_the_tag_is_not_mailed(self):
        docker = FakeDocker({"traefik": Box("traefik:v3.7", "t", "traefik", "traefik")}, versions={"t": "v3.7.19"})
        self.assertEqual(self.notices(docker, [release("v3.7.20", 0)]), [])

    def test_a_new_major_past_a_major_tag_is_mailed(self):
        docker = FakeDocker({"uptime-kuma": Box("louislam/uptime-kuma:2", "k", "uptime-kuma", "uptime-kuma")}, versions={"k": ""})
        docker.files[("uptime-kuma", "k")] = b'{"version": "2.5.9"}'
        self.assertEqual(self.notices(docker, [release("2.6.0", 0)]), [])
        self.assertEqual(
            self.notices(docker, [release("2.6.0", 0), release("3.0.0", 0)]),
            [Notice("uptime-kuma", "2.5.9", "3.0.0", "https://github.com/louislam/uptime-kuma/releases/tag/3.0.0")],
        )

    def test_latest_is_not_checked(self):
        docker = FakeDocker({"homer": Box("b4bz/homer:latest", "h", "homer", "homer")}, versions={"h": "v26.11.2"})
        self.assertEqual(self.notices(docker, [release("v26.11.4", 0)]), [])
        self.github.assert_not_called()

    def test_a_variant_tag_reads_releases_of_that_variant(self):
        docker = FakeDocker({"mosquitto": Box("eclipse-mosquitto:2.1-alpine", "mq", "zigbee2mqtt", "mosquitto")}, versions={"mq": "2.1.4"})
        notices = self.notices(docker, hub=[release("2.1.5", 0), release("2.2.0", 0)])
        self.hub.assert_called_once_with("library/eclipse-mosquitto", "-alpine")
        self.assertEqual(notices, [Notice("mosquitto", "2.1.4", "2.2.0", "https://hub.docker.com/_/eclipse-mosquitto/tags")])

    def test_a_digest_pinned_tag_is_read_without_the_digest(self):
        docker = FakeDocker({"zigbee2mqtt": Box("ghcr.io/koenkk/zigbee2mqtt:2.14@sha256:00", "z", "zigbee2mqtt", "zigbee2mqtt")}, versions={"z": "2.14.3"})
        self.assertEqual([notice.available for notice in self.notices(docker, [release("2.15.0", 0)])], ["2.15.0"])

    def test_hub_tags_keep_only_the_variant(self):
        import releases

        names = ["2.1.4-alpine", "2.1.4", "2.2.0", "2.2-alpine", "latest", "2.1.4-openssl"]
        payload = {"results": [{"name": name} for name in names], "next": None}
        with mock.patch.object(releases, "_get_json", return_value=payload):
            self.assertEqual([tag.tag for tag in releases.hub_tags("library/eclipse-mosquitto", "-alpine")], ["2.1.4-alpine", "2.2-alpine"])
            self.assertEqual([tag.tag for tag in releases.hub_tags("library/eclipse-mosquitto")], ["2.1.4", "2.2.0"])


class HoldMajorTests(unittest.TestCase):
    """linuxserver images follow latest; a pulled image of another major is mailed, not started."""

    IMAGE = "lscr.io/linuxserver/radarr:latest"

    def radarr(self, new_label: str) -> FakeDocker:
        return FakeDocker(
            {"radarr": Box(self.IMAGE, "old", "media-automation", "radarr")},
            {self.IMAGE: "new"},
            {"old": "6.4.4.10685-ls325", "new": new_label},
        )

    def test_a_new_major_is_mailed_and_the_old_image_keeps_the_tag(self):
        docker = self.radarr("7.0.3.10918-ls330")
        outcome = Outcome()
        bump(docker, "radarr", outcome)
        self.assertFalse([call for call in docker.calls if call[:3] == ("compose", "media-automation", "up")])
        self.assertEqual((docker.boxes["radarr"].image_id, docker.images[self.IMAGE]), ("old", "old"))
        self.assertEqual(outcome.notices, [Notice("radarr", "6.4.4.10685", "7.0.3.10918", "https://github.com/Radarr/Radarr/releases/tag/v7.0.3.10918")])
        self.assertEqual((outcome.bumps, outcome.issues, outcome.rejected), ([], [], []))

    def test_the_same_major_is_installed(self):
        docker = self.radarr("6.5.0.10700-ls326")
        outcome = Outcome()
        bump(docker, "radarr", outcome)
        self.assertEqual(docker.boxes["radarr"].image_id, "new")
        self.assertEqual([(item.before, item.after) for item in outcome.bumps], [("6.4.4.10685-ls325", "6.5.0.10700-ls326")])
        self.assertEqual(outcome.notices, [])

    def test_a_new_image_with_no_readable_version_is_not_installed(self):
        docker = self.radarr("")
        outcome = Outcome()
        bump(docker, "radarr", outcome)
        self.assertEqual((docker.boxes["radarr"].image_id, docker.images[self.IMAGE]), ("old", "old"))
        self.assertIn("could not be compared", outcome.issues[0].detail)

    def test_qbittorrent_reads_its_own_version_from_the_label(self):
        image = "lscr.io/linuxserver/qbittorrent:latest"
        docker = FakeDocker(
            {"qbittorrent": Box(image, "old", "media-automation", "qbittorrent")},
            {image: "new"},
            {"old": "5.3.0_v2.1.2-ls486", "new": "6.0.1_v2.1.3-ls500"},
        )
        outcome = Outcome()
        bump(docker, "qbittorrent", outcome)
        self.assertEqual(outcome.notices, [Notice("qbittorrent", "5.3.0", "6.0.1", "https://hub.docker.com/r/linuxserver/qbittorrent/tags")])

    def test_a_stopped_stack_is_left_alone(self):
        docker = self.radarr("6.5.0.10700-ls326")
        outcome = Outcome()
        bump(docker, "radarr", outcome, running=set())
        self.assertEqual(docker.calls, [])
        self.assertEqual((outcome.bumps, outcome.notices, outcome.issues), ([], [], []))


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


class WeeklyMailTests(StateTests):
    TRAEFIK = Bump("traefik", "v3.7.18", "v3.7.19", "https://example.test/t")
    TUNNEL = Bump("cloudflare-tunnel", "2026.11.0", "2026.11.1", "https://example.test/c")
    ADGUARD = Notice("adguard", "v0.107.79", "v0.107.80", "https://example.test/a")
    HA = Notice("homeassistant", "2026.11.1", "2026.11.2", "https://example.test/h")

    def test_bumps_wait_for_sunday_noon_and_go_once(self):
        self.report(SUNDAY - timedelta(days=6), bumps=[self.TRAEFIK])
        self.report(SUNDAY, bumps=[self.TUNNEL], notices=[self.ADGUARD])
        self.assertEqual(self.subjects(), [])
        self.report(SUNDAY, notices=[self.ADGUARD], weekly=True)
        letter = self.letters[0]
        self.assertEqual(self.subjects(), ["Homelab update"])
        self.assertIn("Traefik", letter.text)
        self.assertIn("Cloudflared", letter.text)
        self.assertIn("AdGuard Home", letter.text)
        self.report(SUNDAY, notices=[self.ADGUARD], weekly=True)
        self.assertEqual(self.subjects(), [])

    def test_two_bumps_of_one_container_collapse(self):
        kept = [Kept("traefik", "v3.7.18", "v3.7.19", "https://example.test/1", date(2026, 11, 10))]
        merged = merge_bumps(kept, [Bump("traefik", "v3.7.19", "v3.7.20", "https://example.test/2")], date(2026, 11, 12))
        self.assertEqual((merged[0].before, merged[0].after, merged[0].since), ("v3.7.18", "v3.7.20", date(2026, 11, 10)))

    def test_failed_weekly_mail_is_retried_next_morning(self):
        self.report(SUNDAY, bumps=[self.TRAEFIK])
        self.send_ok = False
        self.report(SUNDAY, weekly=True)
        self.assertTrue(update.DUE.is_file())
        self.assertTrue(update.BUMPS.is_file())
        self.letters.clear()
        self.send_ok = True
        self.report(SUNDAY + timedelta(days=1))
        self.assertEqual(self.subjects(), ["Containers were updated"])
        self.assertFalse(update.DUE.is_file())
        self.assertFalse(update.BUMPS.is_file())

    def test_a_due_mail_with_nothing_left_is_cleared(self):
        self.send_ok = False
        self.report(SUNDAY, notices=[self.ADGUARD], weekly=True)
        self.send_ok = True
        self.letters.clear()
        self.report(SUNDAY + timedelta(days=1))
        self.assertFalse(update.DUE.is_file())
        self.report(SUNDAY + timedelta(days=2), bumps=[self.TRAEFIK])
        self.assertEqual(self.subjects(), [])

    def test_a_missed_sunday_sends_a_week_old_bump_but_not_on_sunday_morning(self):
        self.report(SUNDAY, bumps=[self.TRAEFIK])
        self.report(SUNDAY, weekly=True)
        self.letters.clear()
        self.report(SUNDAY, bumps=[self.TUNNEL])
        self.report(SUNDAY + timedelta(days=7))
        self.assertEqual(self.subjects(), [])
        self.report(SUNDAY + timedelta(days=8))
        self.assertEqual(self.subjects(), ["Containers were updated"])

    def test_digest_due_rules(self):
        kept = [Kept("traefik", "v3.7.18", "v3.7.19", "https://example.test/t", date(2026, 11, 9))]
        self.assertFalse(digest_due(date(2026, 11, 15), kept, wday=7))
        self.assertTrue(digest_due(date(2026, 11, 16), kept, wday=1))
        self.assertTrue(digest_due(date(2026, 11, 11), [], retry=True, wday=3))
        self.assertFalse(digest_due(date(2026, 11, 22), kept, wday=7))

    def test_a_release_is_not_sent_again_after_a_failed_check(self):
        self.report(SUNDAY, notices=[self.ADGUARD, self.HA], weekly=True)
        self.report(SUNDAY + timedelta(days=2), bumps=[self.TRAEFIK])
        self.report(SUNDAY + timedelta(days=7), notices=[self.HA], weekly=True)
        self.letters.clear()
        self.report(SUNDAY + timedelta(days=14), notices=[self.ADGUARD, self.HA], weekly=True)
        self.assertEqual(self.subjects(), [])

    def test_the_same_release_with_a_new_running_version_is_not_sent_again(self):
        self.report(SUNDAY, notices=[self.ADGUARD], weekly=True)
        self.letters.clear()
        moved = Notice("adguard", "v0.107.79.1", "v0.107.80", "https://example.test/a")
        self.report(SUNDAY + timedelta(days=7), notices=[moved], weekly=True)
        self.assertEqual(self.subjects(), [])

    def test_notify_only_leaves_the_weeks_bumps(self):
        self.report(SUNDAY - timedelta(days=2), bumps=[self.TRAEFIK])
        before = update.BUMPS.read_text()
        update._send_notices([self.ADGUARD], False)
        self.assertEqual(self.subjects(), ["Pinned containers have a newer release"])
        self.assertEqual(update.BUMPS.read_text(), before)
        self.report(SUNDAY, notices=[self.ADGUARD], weekly=True)
        self.assertNotIn("AdGuard", self.letters[0].text)


class FailureMailTests(StateTests):
    PULL = Issue("uptime-kuma", "Pull of louislam/uptime-kuma:2 failed. The running container was not changed.")
    HEALTH = Issue("traefik", "The new image did not become healthy. The previous image was restored.")

    def test_a_failure_is_mailed_once_and_a_different_one_again(self):
        day = date(2026, 11, 16)
        self.report(day, issues=[self.PULL])
        self.assertEqual(self.subjects(), ["Container update failed"])
        self.report(day + timedelta(days=1), issues=[self.PULL])
        self.assertEqual(self.subjects(), [])
        self.report(day + timedelta(days=2), issues=[self.PULL, self.HEALTH])
        letter = self.letters[0]
        self.assertEqual(self.subjects(), ["Container update failed"])
        self.assertIn("Traefik", letter.text)
        self.assertNotIn("Uptime Kuma", letter.text)

    def test_a_failure_with_line_breaks_is_still_mailed_once(self):
        issue = Issue("traefik", "Stopped with an unexpected error. 'Config'\nTraceback line")
        self.report(date(2026, 11, 16), issues=[issue])
        self.report(date(2026, 11, 17), issues=[issue])
        self.assertEqual(self.subjects(), ["Container update failed"])

    def test_a_failure_mail_that_did_not_send_is_retried(self):
        day = date(2026, 11, 16)
        self.send_ok = False
        self.report(day, issues=[self.PULL])
        self.send_ok = True
        self.letters.clear()
        self.report(day + timedelta(days=1), issues=[self.PULL])
        self.assertEqual(self.subjects(), ["Container update failed"])

    def test_the_failure_mail_goes_out_even_if_state_cannot_be_written(self):
        with mock.patch.object(update, "_save_bumps", side_effect=PermissionError("state is read-only")):
            with self.assertRaises(PermissionError):
                self.report(date(2026, 11, 16), bumps=[WeeklyMailTests.TRAEFIK], issues=[self.PULL])
        self.assertEqual(self.subjects(), ["Container update failed"])

    def test_a_release_check_failing_three_mornings_is_mailed_once(self):
        day = date(2026, 11, 16)
        for offset in range(2):
            issues = update._unchecked_issues([("adguard", "rate limited")], day + timedelta(days=offset))
            self.assertEqual(issues, [])
        third = update._unchecked_issues([("adguard", "HTTP 500")], day + timedelta(days=2))
        self.assertEqual([issue.name for issue in third], ["adguard"])
        self.assertIn("since 2026-11-16", third[0].detail)
        self.report(day + timedelta(days=2), issues=third)
        self.assertEqual(self.subjects(), ["Container update failed"])
        fourth = update._unchecked_issues([("adguard", "timed out")], day + timedelta(days=3))
        self.report(day + timedelta(days=3), issues=fourth)
        self.assertEqual(self.subjects(), [])

    def test_a_release_check_that_works_again_starts_over(self):
        day = date(2026, 11, 16)
        update._unchecked_issues([("plex", "down")], day)
        update._unchecked_issues([("plex", "down")], day + timedelta(days=1))
        self.assertEqual(update._unchecked_issues([], day + timedelta(days=2)), [])
        self.assertFalse(update.UNCHECKED.is_file())
        self.assertEqual(update._unchecked_issues([("plex", "down")], day + timedelta(days=3)), [])

    def test_every_failure_is_logged_each_morning_with_its_error(self):
        day = date(2026, 11, 16)
        for offset in range(2):
            with mock.patch.object(update, "log") as log:
                self.report(day + timedelta(days=offset), issues=[self.PULL])
            self.assertIn(mock.call(f"uptime-kuma: {self.PULL.detail}"), log.call_args_list)

    def test_the_weekly_mail_never_carries_failures(self):
        update._mark_due(date(2026, 11, 15))
        self.report(date(2026, 11, 16), bumps=[WeeklyMailTests.TRAEFIK], issues=[self.PULL])
        failure, weekly = self.letters
        self.assertEqual(failure.subject, "Container update failed")
        self.assertNotIn("Traefik", failure.text)
        self.assertNotIn("Failed", weekly.text)
        self.assertNotIn("Uptime Kuma", weekly.text)


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


class MainTests(MainCase):
    def test_only_the_weekly_run_prunes(self):
        update.main([])
        self.assertEqual(self.docker.pruned, 0)
        update.main(["--dry-run", "--digest"])
        self.assertEqual(self.docker.pruned, 0)
        update.main(["--notify-only"])
        self.assertEqual(self.docker.pruned, 0)
        update.main(["--digest"])
        self.assertEqual(self.docker.pruned, 1)
        self.assertFalse(update.LOCK.exists())

    def test_a_failed_restore_holds_the_weekly_prune(self):
        self.outcome = Outcome(hold=["traefik\tRestoring the previous image failed."])
        update.main([])
        self.assertTrue(update.HOLD.is_file())
        self.letters.clear()
        update.main(["--digest"])
        self.assertEqual(self.docker.pruned, 0)
        self.outcome = Outcome()
        update.main([])
        self.assertEqual(self.subjects(), ["Container update failed"])
        self.assertIn("not pruned on 2026-11-15", update.PRUNE_ERROR.read_text())
        with mock.patch.dict(os.environ, {"UPDATE_TODAY": "2026-11-22"}):
            update.main(["--digest"])
            update.main([])
        self.assertEqual(self.subjects(), ["Container update failed"])
        update.HOLD.unlink()
        update.main(["--digest"])
        self.assertEqual(self.docker.pruned, 1)
        self.assertFalse(update.PRUNE_ERROR.is_file())

    def test_a_stopped_run_keeps_what_already_moved(self):
        def stopped(_docker, _config, _dry, _rejected, outcome, _judge=None):
            outcome.bumps.append(Bump("traefik", "v3.7.18", "v3.7.19"))
            outcome.rejected.append(("uptime-kuma", "sha256:bad"))
            raise SystemExit(143)

        self.run.side_effect = stopped
        with self.assertRaises(SystemExit):
            update.main([])
        self.assertIn("traefik\tv3.7.18\tv3.7.19", update.BUMPS.read_text())
        self.assertIn("uptime-kuma\tsha256:bad", update.REJECTED.read_text())
        self.assertFalse(update.LOCK.exists())

    def test_rolled_back_images_are_remembered(self):
        self.outcome = Outcome(rejected=[("traefik", "sha256:new")])
        update.main([])
        self.outcome = Outcome()
        update.main([])
        self.assertEqual(self.run.call_args.args[3], {("traefik", "sha256:new")})

    def test_a_failed_prune_is_mailed_the_next_morning(self):
        self.docker.prune_error = "daemon busy"
        update.main(["--digest"])
        self.assertEqual(self.subjects(), [])
        update.main([])
        self.assertEqual(self.subjects(), ["Container update failed"])
        self.docker.prune_error = None
        update.main(["--digest"])
        self.assertFalse(update.PRUNE_ERROR.is_file())

    def test_a_held_lock_moves_the_weekly_mail_to_the_morning(self):
        update.LOCK.mkdir(parents=True)
        self.assertEqual(update.main(["--digest"]), 0)
        self.assertTrue(update.DUE.is_file())
        self.assertEqual(self.docker.pruned, 0)
        update.main([])
        self.run.assert_not_called()

    def test_a_stale_lock_is_cleared_and_mailed(self):
        update.LOCK.mkdir(parents=True)
        old = time.time() - update.LOCK_STALE - 60
        os.utime(update.LOCK, (old, old))
        update.main([])
        self.run.assert_called_once()
        self.assertIn("did not finish", self.letters[0].text)
        self.assertFalse(update.LOCK.exists())

    def test_an_error_still_releases_the_lock(self):
        self.run.side_effect = RuntimeError("boom")
        with self.assertRaises(RuntimeError):
            update.main([])
        self.assertFalse(update.LOCK.exists())


class PushTests(MainCase):
    """Each update that held is pushed to ntfy that morning, and still kept for the weekly mail."""

    BUMPS = [
        Bump("traefik", "3.7.19", "3.7.20", "https://github.com/traefik/traefik/releases/tag/v3.7.20"),
        Bump("mosquitto", "2.1.4", "2.1.5", "https://hub.docker.com/_/eclipse-mosquitto/tags"),
    ]

    real_send = staticmethod(push.send)

    def setUp(self):
        super().setUp()
        self.outcome.bumps.extend(self.BUMPS)
        patcher = mock.patch.dict(os.environ, {"UPDATE_TODAY": "2026-11-16", "UPDATE_WDAY": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def env(self, text: str) -> None:
        (self.dir / ".env").write_text(text)

    def test_every_update_is_pushed_and_kept_for_the_weekly_mail(self):
        self.env("RESEND_API_KEY=k\nNTFY_TOPIC='secret-topic'\n")
        self.assertEqual(update.main([]), 0)
        self.assertEqual(self.pushes, [(bump, "secret-topic", "https://ntfy.sh") for bump in self.BUMPS])
        self.assertEqual([kept.key for kept in update._load_bumps()], ["traefik", "mosquitto"])
        self.assertEqual(self.letters, [])

    def test_another_server_can_be_set(self):
        self.env("NTFY_TOPIC=t\nNTFY_URL=https://ntfy.example.com/\n")
        update.main([])
        self.assertEqual({server for _bump, _topic, server in self.pushes}, {"https://ntfy.example.com/"})

    def test_no_topic_or_no_env_file_pushes_nothing(self):
        update.main([])
        self.env("RESEND_API_KEY=k\n")
        update.main([])
        self.assertEqual(self.pushes, [])
        self.assertEqual(len(update._load_bumps()), 2)

    def test_a_failed_push_does_not_fail_the_run(self):
        self.env("NTFY_TOPIC=t\n")
        self.push_error = RuntimeError("ntfy returned HTTP 429")
        self.assertEqual(update.main([]), 0)
        self.assertEqual(len(update._load_bumps()), 2)

    def test_a_dry_run_and_the_sunday_mail_push_nothing(self):
        self.env("NTFY_TOPIC=t\n")
        update.main(["--dry-run"])
        update.main(["--digest"])
        self.assertEqual(self.pushes, [])

    def test_the_message(self):
        mail.use_titles(CONFIG.titles)
        notes = "https://github.com/traefik/traefik/releases/tag/v3.7.20"
        icon = "https://raw.githubusercontent.com/helderjgoncalves/Homelab-Infrastructure/main/updates/icons/traefik.png"
        self.assertEqual(push.message(self.BUMPS[0], "t"), {
            "topic": "t", "title": "Traefik 3.7.19 → 3.7.20", "message": "Updated and running.",
            "click": notes, "actions": [{"action": "view", "label": "Release notes", "url": notes, "clear": True}],
            "icon": icon, "attach": icon, "filename": "traefik.png",
        })
        bare = push.message(Bump("updater", "", ""), "t")
        self.assertEqual((bare["title"], bare["message"]), ("Updater updated", "Updated and running."))
        self.assertFalse({"click", "actions", "icon", "attach"} & set(bare))
        rebuild = push.message(Bump("cloudflare-tunnel", "2026.11.0", "2026.11.0"), "t")
        self.assertEqual((rebuild["title"], rebuild["message"]), ("Cloudflared 2026.11.0", "New build of the same version."))
        self.assertEqual(push.message(Bump("seerr", "", "v3.5.2"), "t")["title"], "Seerr v3.5.2")

    def test_every_app_icon_is_in_the_repo(self):
        missing = [key for key in APPS if not (mail.ICONS / f"{key}.png").is_file()]
        self.assertEqual(missing, [])

    def test_send_posts_the_json_to_the_server_root(self):
        response = mock.MagicMock(status=200)
        response.__enter__.return_value = response
        with mock.patch.object(urllib.request, "urlopen", return_value=response) as urlopen:
            self.real_send(self.BUMPS[0], "t", "https://ntfy.sh/")
        request = urlopen.call_args.args[0]
        self.assertEqual((request.full_url, request.get_method()), ("https://ntfy.sh", "POST"))
        self.assertEqual(json.loads(request.data)["topic"], "t")


class ReleasePushTests(MainCase):
    """A release to install by hand is pushed once, the first morning it is seen."""

    RADARR = Notice("radarr", "6.4.4.10685", "7.0.3.10918", "https://github.com/Radarr/Radarr/releases/tag/v7.0.3.10918")
    IMMICH = Notice("immich", "3.4.1", "3.5.0", "https://github.com/immich-app/immich/releases/tag/v3.5.0", by_hand=False)

    def setUp(self):
        super().setUp()
        self.outcome.notices.extend([self.RADARR, self.IMMICH])
        (self.dir / ".env").write_text("NTFY_TOPIC=t\n")
        patcher = mock.patch.dict(os.environ, {"UPDATE_TODAY": "2026-11-16", "UPDATE_WDAY": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def pushed(self) -> list:
        return [item for item, _topic, _server in self.pushes]

    def test_a_release_is_pushed_once_not_every_morning(self):
        self.assertEqual(update.main([]), 0)
        update.main([])
        self.assertEqual(self.pushed(), [self.RADARR])
        self.assertEqual(self.letters, [])

    def test_a_newer_release_of_the_same_app_is_pushed_again(self):
        update.main([])
        newer = Notice("radarr", "6.4.4.10685", "7.0.4.10950", self.RADARR.url)
        self.outcome.notices[:] = [newer]
        update.main([])
        self.assertEqual(self.pushed(), [self.RADARR, newer])

    def test_a_failed_push_is_tried_again_the_next_morning(self):
        self.push_error = RuntimeError("ntfy returned HTTP 429")
        self.assertEqual(update.main([]), 0)
        self.push_error = None
        update.main([])
        self.assertEqual(self.pushed(), [self.RADARR])

    def test_a_release_already_mailed_is_not_pushed(self):
        update._remember_notices([self.RADARR])
        update.main([])
        self.assertEqual(self.pushes, [])

    def test_the_weekly_mail_clears_what_was_pushed(self):
        update.main([])
        self.assertEqual(update._pushed(), {("radarr", "7.0.3.10918"): ""})
        update._remember_notices([self.RADARR])
        update.main([])
        self.assertEqual((update._pushed(), self.pushed()), ({}, [self.RADARR]))

    def test_a_release_pushed_before_workers_ai_answered_is_pushed_once_more_with_the_answer(self):
        waiting = replace(self.RADARR, verdict="manual", reasons=("Workers AI was not asked: it has no tokens left today (HTTP 429).",), unanswered=True)
        self.outcome.notices[:] = [waiting]
        update.main([])
        self.assertEqual(update._pushed(), {("radarr", "7.0.3.10918"): "unanswered"})
        update.main([])
        update._remember_notices([waiting])
        answered = replace(self.RADARR, verdict="manual", reasons=("The database is migrated.",), actions=("Back up the database",))
        self.outcome.notices[:] = [answered]
        update.main([])
        update.main([])
        self.assertEqual(self.pushed(), [waiting, answered])
        self.assertIn("The database is migrated.", push.message(self.pushed()[1], "t")["message"])

    def test_an_unanswered_release_is_forgotten_once_a_newer_one_is_listed(self):
        self.outcome.notices[:] = [replace(self.RADARR, unanswered=True)]
        update.main([])
        self.outcome.notices[:] = []
        update.main([])
        self.assertEqual(update._pushed(), {("radarr", "7.0.3.10918"): "unanswered"})
        self.outcome.notices[:] = [replace(self.RADARR, available="7.0.4.11000")]
        update.main([])
        self.assertEqual(update._pushed(), {("radarr", "7.0.4.11000"): ""})

    def test_an_unanswered_release_left_out_of_the_sunday_mail_is_not_pushed_again(self):
        self.outcome.notices[:] = [replace(self.RADARR, unanswered=True)]
        update.main([])
        with mock.patch.object(update, "collect_notices", lambda *_args, **_kwargs: None):
            update.main(["--digest"])
        update.main([])
        self.assertEqual(len(self.pushes), 1)

    def test_a_release_first_seen_on_sunday_is_pushed_and_mailed(self):
        self.outcome.notices.clear()
        with mock.patch.object(update, "collect_notices", lambda _docker, _config, _running, notices, _judge=None: notices.append(self.RADARR)):
            update.main(["--digest"])
        self.assertEqual(self.pushed(), [self.RADARR])
        self.assertEqual(len(self.letters), 1)

    def test_a_dry_run_pushes_and_keeps_nothing(self):
        update.main(["--dry-run"])
        self.assertEqual((self.pushes, update.PUSHED.exists()), ([], False))

    def test_the_message(self):
        mail.use_titles(CONFIG.titles)
        icon = "https://raw.githubusercontent.com/helderjgoncalves/Homelab-Infrastructure/main/updates/icons/radarr.png"
        self.assertEqual(push.message(self.RADARR, "t"), {
            "topic": "t", "title": "Radarr 7.0.3.10918 is out", "message": "You run 6.4.4.10685. A new major, install it by hand.",
            "click": self.RADARR.url, "actions": [{"action": "view", "label": "Release notes", "url": self.RADARR.url, "clear": True}],
            "icon": icon, "attach": icon, "filename": "radarr.png",
        })
        minor = push.message(Notice("adguard", "0.107.79", "0.108.0", "https://github.com/AdguardTeam/AdGuardHome/releases/tag/v0.108.0"), "t")
        self.assertEqual((minor["title"], minor["message"]), ("AdGuard Home 0.108.0 is out", "You run 0.107.79. Install it by hand."))
        plex = push.message(Notice("plex", "1.42.1.10060", "1.42.2.10156", jobs.PLEX_DOWNLOADS), "t")
        self.assertEqual(plex["actions"][0]["label"], "Open")

    def test_only_a_minor_that_waits_installs_by_itself(self):
        now = datetime.now(timezone.utc)
        notices: list = []
        jobs._waiting_notice(APPS["immich"], "v3.4.1", release("v3.5.0", 0, now=now), notices)
        jobs._waiting_notice(APPS["adguard"], "v0.107.79", release("v0.108.0", 0, now=now), notices)
        jobs._waiting_notice(APPS["immich"], "v3.4.1", release("v4.0.0", 0, now=now), notices)
        undated = Release((3, 5, 0), "v3.5.0", None)
        jobs._waiting_notice(APPS["immich"], "v3.4.1", undated, notices)
        self.assertEqual([notice.by_hand for notice in notices], [False, True, True, True])


class MailTests(unittest.TestCase):
    def test_pinned_letter_uses_the_card_and_escapes(self):
        letter = pinned_letter([Notice("adguard", "v0.107.79", "v0.107.80<script>", "https://example.test/adguard")])
        self.assertIn("Newer releases", letter.html)
        self.assertIn("linear-gradient(90deg,#ffd27a,#ff9a4d,#ff6f91)", letter.html)
        self.assertIn("AdGuard Home", letter.html)
        self.assertNotIn("<script>", letter.html)
        self.assertIn("v0.107.80", letter.text)
        self.assertIn("Nothing was changed", letter.text)

    def test_immich_shows_running_and_available(self):
        letter = pinned_letter([Notice("immich", "3.4.1", "3.5.0", "https://github.com/immich-app/immich/releases/tag/v3.5.0")])
        self.assertIn("Immich", letter.html)
        self.assertIn("3.4.1", letter.html)
        self.assertIn("3.5.0", letter.html)
        self.assertIn("white-space:nowrap", letter.html)

    def test_plex_build_number_counts(self):
        current = parse_version("1.43.5.11079-b3b13e506".split("-", 1)[0])
        self.assertFalse(newer(parse_version("1.43.5.11079"), current))
        self.assertTrue(newer(parse_version("1.43.5.12000"), current))

    def test_weekly_letter_has_bumps_then_releases(self):
        letter = pinned_letter(
            [Notice("adguard", "v0.107.79", "v0.107.80", "https://example.test/adguard")],
            [
                Bump("traefik", "v3.7.18", "v3.7.19", "https://github.com/traefik/traefik/releases/tag/v3.7.19"),
                Bump("cloudflare-tunnel", "latest", "latest"),
            ],
        )
        self.assertEqual(letter.subject, "Homelab update")
        self.assertLess(letter.html.index("Updated"), letter.html.index("Newer releases"))
        self.assertIn('src="cid:traefik"', letter.html)
        self.assertIn("v3.7.18 → v3.7.19", letter.text)
        self.assertNotIn("latest → latest", letter.html)

    def test_failure_letter_names_the_container(self):
        letter = failure_letter([Issue("traefik", "The new image did not become healthy. The previous image was restored.")])
        self.assertEqual(letter.subject, "Container update failed")
        self.assertIn("Traefik", letter.html)
        self.assertIn('src="cid:traefik"', letter.html)
        self.assertIn("previous image was restored", letter.text)

    def test_api_version_follows_the_daemon_minimum(self):
        self.assertEqual(api_prefix({"ApiVersion": "1.46", "MinAPIVersion": "1.24"}), "/v1.41")
        self.assertEqual(api_prefix({"ApiVersion": "1.52", "MinAPIVersion": "1.44"}), "/v1.44")
        self.assertEqual(api_prefix({}), "/v1.41")

    def test_prune_covers_every_unused_image(self):
        path = unused_image_prune_path()
        self.assertIn("/images/prune?", path)
        self.assertIn('"dangling":["false"]', urllib.parse.unquote(path))


QTS_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<docRoot><storage>
  <model>
    <productName>TS-1264U-RP,TS-464U</productName>
    <modelName>TS-X64U</modelName>
    <firmware><version>5.2.12</version><number>3700</number><buildNumber>20261201</buildNumber></firmware>
  </model>
  <model>
    <productName>TS-264,TS-464,TS-664</productName>
    <modelName>TS-X64</modelName>
    <firmware>
      <version>5.2.11</version><number>3683</number><buildNumber>20261114</buildNumber>
      <updateInfoURL>https://static.myqnapcloud.com/qts_release_note/5.2.11.3683/qts_note.html</updateInfoURL>
    </firmware>
  </model>
</storage></docRoot>
"""


class QtsTests(unittest.TestCase):
    APP = APPS["qts"]
    RUNNING = {
        "QTS_VERSION": "5.2.11", "QTS_NUMBER": "3683", "QTS_BUILD": "20261114",
        "QTS_MODEL": "TS-X64", "QTS_FEED": "https://update.qnap.com/QTS_FW_5.2.0.xml",
    }

    def check(self, env: dict[str, str], found=None, error: Exception | None = None):
        notices: list[Notice] = []
        unchecked: list[tuple[str, str]] = []
        public = mock.patch.object(jobs, "qts_public", side_effect=error, return_value=found)
        with mock.patch.dict(os.environ, env), public as fetched:
            jobs._qts(self.APP, notices, unchecked)
        return notices, unchecked, fetched

    def test_the_feed_entry_for_the_model_is_read(self):
        found = qts_firmware(QTS_FEED, "TS-X64")
        self.assertEqual(found, Firmware("5.2.11", "3683", "20261114", "https://static.myqnapcloud.com/qts_release_note/5.2.11.3683/qts_note.html"))
        self.assertEqual(found.label, "5.2.11.3683 build 20261114")
        self.assertEqual(qts_firmware(QTS_FEED, "TS-464U").version, "5.2.12")
        self.assertIsNone(qts_firmware(QTS_FEED, "TS-X53"))

    def test_the_running_build_is_current(self):
        notices, unchecked, _fetched = self.check(self.RUNNING, qts_firmware(QTS_FEED, "TS-X64"))
        self.assertEqual((notices, unchecked), ([], []))

    def test_a_newer_firmware_is_a_notice(self):
        found = Firmware("5.2.12", "3700", "20261201", "https://example.test/notes")
        notices, _unchecked, fetched = self.check(self.RUNNING, found)
        fetched.assert_called_once_with("https://update.qnap.com/QTS_FW_5.2.0.xml", "TS-X64")
        self.assertEqual(notices, [Notice("qts", "5.2.11.3683 build 20261114", "5.2.12.3700 build 20261201", "https://example.test/notes")])

    def test_a_rebuild_of_the_same_version_counts(self):
        notices, _unchecked, _fetched = self.check(self.RUNNING, Firmware("5.2.11", "3683", "20261120"))
        self.assertEqual(notices, [Notice("qts", "5.2.11.3683 build 20261114", "5.2.11.3683 build 20261120", jobs.QTS_DOWNLOADS)])

    def test_an_older_feed_is_not_a_notice(self):
        notices, _unchecked, _fetched = self.check(self.RUNNING, Firmware("5.2.10", "3650", "20261001"))
        self.assertEqual(notices, [])

    def test_off_a_qnap_nothing_is_fetched(self):
        notices, unchecked, fetched = self.check({"QTS_VERSION": ""})
        fetched.assert_not_called()
        self.assertEqual((notices, unchecked), ([], []))
        _notices, _unchecked, fetched = self.check({**self.RUNNING, "QTS_FEED": ""})
        fetched.assert_not_called()

    def test_a_failed_check_or_a_missing_model_is_unchecked(self):
        _notices, unchecked, _fetched = self.check(self.RUNNING, error=OSError("feed down"))
        self.assertEqual(unchecked, [("qts", "feed down")])
        _notices, unchecked, _fetched = self.check(self.RUNNING, None)
        self.assertEqual(unchecked, [("qts", "no firmware for TS-X64")])

    def test_the_mail_row_has_the_title_and_both_builds(self):
        letter = pinned_letter([Notice("qts", "5.2.11.3683 build 20261114", "5.2.12.3700 build 20261201", "https://example.test/notes")])
        self.assertIn("QTS", letter.html)
        self.assertIn("5.2.12.3700 build 20261201", letter.text)
        self.assertIn('src="cid:qts"', letter.html)


class SameAsBeforeConfigTests(unittest.TestCase):
    """config.toml makes the same decisions as the tables it replaced, copied here as they were."""

    FLOATING = {"cloudflare-tunnel": None, "traefik": None, "uptime-kuma": None}
    GITHUB = {"opencloud-opencloud-1": "opencloud-eu/opencloud"}
    HUB: dict[str, str] = {}
    IMMEDIATE = {
        "traefik", "cloudflare-tunnel", "beszel", "beszel-agent", "uptime-kuma", "dockge",
        "homeassistant", "zigbee2mqtt", "mosquitto", "homer",
        "radarr", "sonarr", "prowlarr", "qbittorrent", "maintainerr", "seerr",
    }
    HOLD_MAJOR = {"radarr", "sonarr", "prowlarr", "qbittorrent"}
    IMMICH = ("immich_server", "immich_machine_learning")
    APP_VERSION = {"uptime-kuma": "/app/package.json", "dockge": "/app/package.json", "maintainerr": "/opt/app/apps/server/package.json"}
    VERSION_COMMAND = {"cloudflare-tunnel": ("cloudflared", "--version")}
    TITLES = {
        "adguard": "AdGuard Home", "homeassistant": "Home Assistant", "zigbee2mqtt": "Zigbee2MQTT",
        "mosquitto": "Mosquitto", "opencloud-opencloud-1": "OpenCloud", "traefik": "Traefik",
        "cloudflare-tunnel": "Cloudflared", "beszel": "Beszel", "uptime-kuma": "Uptime Kuma",
        "dockge": "Dockge", "immich": "Immich", "plex": "Plex", "qts": "QTS", "homer": "Homer",
        "radarr": "Radarr", "sonarr": "Sonarr", "prowlarr": "Prowlarr", "qbittorrent": "qBittorrent",
        "maintainerr": "Maintainerr", "seerr": "Seerr",
    }
    RELEASES = {
        "traefik": ("https://github.com/traefik/traefik/releases", True),
        "cloudflare-tunnel": ("https://github.com/cloudflare/cloudflared/releases", False),
        "beszel": ("https://github.com/henrygd/beszel/releases", True),
        "uptime-kuma": ("https://github.com/louislam/uptime-kuma/releases", False),
        "adguard": ("https://github.com/AdguardTeam/AdGuardHome/releases", True),
        "dockge": ("https://github.com/louislam/dockge/releases", False),
        "immich": ("https://github.com/immich-app/immich/releases", True),
    }

    @classmethod
    def old_release_url(cls, key: str, version: str) -> str:
        base, prefixed = cls.RELEASES[key]
        text = version.strip()
        if not text or text in {"latest", "v3", "0", "1", "2"}:
            return base
        tag = text if text.startswith("v") or not prefixed else f"v{text}"
        if not prefixed:
            tag = text[1:] if text.startswith("v") else text
        return f"{base}/tag/{tag}"

    def by_policy(self, policy: str) -> dict:
        return {key: app for key, app in APPS.items() if app.policy == policy}

    def test_the_same_containers_update_in_the_morning(self):
        floating = self.by_policy("floating")
        self.assertEqual({name for app in floating.values() for name in app.containers}, self.IMMEDIATE)
        self.assertEqual({key: app.image or None for key, app in floating.items() if key in self.FLOATING}, self.FLOATING)
        self.assertEqual(APPS["beszel"].containers, ("beszel", "beszel-agent"))
        self.assertEqual(CONFIG.compose_container, "dockge")
        self.assertEqual({key for key, app in APPS.items() if app.hold_major}, self.HOLD_MAJOR)

    def test_immich_and_adguard_are_the_semver_apps(self):
        self.assertEqual(list(self.by_policy("semver")), ["immich", "adguard"])
        self.assertEqual(APPS["immich"].containers, self.IMMICH)
        self.assertEqual(APPS["immich"].releases, "github:immich-app/immich")
        self.assertEqual((APPS["immich"].pin_minor, APPS["adguard"].pin_minor), (False, True))
        self.assertEqual(APPS["adguard"].releases, "github:AdguardTeam/AdGuardHome")

    def test_the_same_pinned_apps_are_checked(self):
        reported = {app.main: app.releases for app in self.by_policy("report").values() if app.releases not in ("plex", "qts")}
        expected = {name: f"github:{repo}" for name, repo in self.GITHUB.items()}
        expected.update({name: f"hub:{repo}" for name, repo in self.HUB.items()})
        self.assertEqual(reported, expected)
        self.assertEqual(APPS["plex"].releases, "plex")
        self.assertEqual(APPS["qts"].releases, "qts")
        self.assertEqual({key for key, app in APPS.items() if app.policy == "report"}, set(expected) | {"plex", "qts"})

    def test_titles_and_version_sources_are_unchanged(self):
        self.assertEqual(CONFIG.titles, self.TITLES)
        self.assertEqual({key: app.version_file for key, app in APPS.items() if app.version_file}, self.APP_VERSION)
        self.assertEqual({key: app.version_command for key, app in APPS.items() if app.version_command}, self.VERSION_COMMAND)

    def test_release_links_are_unchanged(self):
        versions = ("", "latest", "v3", "0", "1", "2", "2.5.11", "v2.5.11", "3.7.19", "v3.7.19", "2026.11.0", "1.5.0")
        for key in self.RELEASES:
            for version in versions:
                with self.subTest(key=key, version=version):
                    self.assertEqual(jobs.release_url(APPS[key], version), self.old_release_url(key, version))
        self.assertEqual(jobs._release_page(APPS["mosquitto"], "2.0.22"), "https://hub.docker.com/_/eclipse-mosquitto/tags")
        self.assertEqual(jobs._release_page(APPS["adguard"], "v0.107.80"), "https://github.com/AdguardTeam/AdGuardHome/releases/tag/v0.107.80")

    def test_steps_run_in_the_same_order_and_dockge_last(self):
        order = []
        with mock.patch.object(jobs, "update_floating", lambda *args, **_kwargs: order.append(args[-1].key)), \
                mock.patch.object(jobs, "update_semver", lambda *args, **_kwargs: order.append(args[-1].key)), \
                mock.patch.object(jobs, "collect_pinned", lambda *args: order.append("pinned")), \
                mock.patch.object(jobs, "collect_outside", lambda *args: order.append("outside")):
            run(FakeDocker({}), CONFIG, False)
        self.assertEqual(order, [
            "immich", "cloudflare-tunnel", "traefik", "uptime-kuma", "beszel", "adguard",
            "homeassistant", "zigbee2mqtt", "mosquitto", "homer",
            "radarr", "sonarr", "prowlarr", "qbittorrent", "maintainerr", "seerr", "pinned", "outside", "dockge",
        ])


class ConfigTests(unittest.TestCase):
    BASE = """
compose_container = "dockge"

[apps.traefik]
title = "Traefik"
policy = "floating"
containers = ["traefik"]

[apps.dockge]
title = "Dockge"
policy = "floating"
containers = ["dockge"]
"""

    def load(self, text: str):
        path = Path(tempfile.mkdtemp()) / "config.toml"
        self.addCleanup(shutil.rmtree, path.parent)
        path.write_text(text)
        return config.load(path)

    def rejects(self, text: str, message: str) -> None:
        with self.assertRaises(ConfigError) as caught:
            self.load(text)
        self.assertIn(message, str(caught.exception))

    def test_the_shipped_config_loads(self):
        self.assertIn("traefik", APPS)

    def test_a_minimal_config_loads(self):
        loaded = self.load(self.BASE)
        self.assertEqual(list(loaded.apps), ["traefik", "dockge"])
        self.assertEqual(loaded.apps["traefik"].main, "traefik")

    def test_mistakes_stop_the_run(self):
        app = '\n[apps.x]\ntitle = "X"\n'
        cases = [
            ("compose_container = ", "config.toml:"),
            ("colour = 1\n" + self.BASE, "unknown setting colour"),
            (self.BASE.replace('compose_container = "dockge"', ""), "compose_container"),
            ('compose_container = "dockge"\n', "no [apps.<key>] tables"),
            (self.BASE + app + 'policy = "floating"\ncontainers = ["x"]\npolicey = 1\n', "apps.x: unknown field policey"),
            (self.BASE + app + 'policy = "auto"\ncontainers = ["x"]\n', "apps.x.policy must be one of"),
            (self.BASE + '\n[apps.x]\npolicy = "floating"\ncontainers = ["x"]\n', "apps.x.title"),
            (self.BASE + app + 'policy = "report"\ncontainers = ["x"]\n', "a report app needs releases"),
            (self.BASE + app + 'policy = "report"\ncontainers = ["x"]\nreleases = "gitlab:a/b"\n', "apps.x.releases must be"),
            (self.BASE + app + 'policy = "floating"\ncontainers = []\n', "apps.x.containers is empty"),
            (self.BASE + app + 'policy = "floating"\ncontainers = ["traefik"]\n', "container traefik is in apps.traefik and apps.x"),
            (self.BASE + app + 'policy = "floating"\ncontainers = ["x", "y"]\nimage = "x:1"\n', "apps.x.image"),
            (self.BASE + app + 'policy = "report"\ncontainers = ["x"]\nreleases = "plex"\n', "plex is the QNAP package"),
            (self.BASE + app + 'policy = "semver"\ncontainers = ["x"]\nreleases = "hub:x/y"\n', "follows GitHub releases"),
            (self.BASE + app + 'policy = "floating"\ncontainers = ["x"]\nversion_file = "app/package.json"\n', "absolute path"),
            (self.BASE + app + 'policy = "floating"\ncontainers = ["x"]\nversion_file = "/a"\nversion_command = ["a"]\n', "not both"),
            (self.BASE + app + 'policy = "report"\ncontainers = ["x"]\nreleases = "github:a/b"\nversion_file = "/a"\n', "for floating apps"),
            (self.BASE + app + 'policy = "floating"\ncontainers = ["x", "x"]\n', "names a container twice"),
            (self.BASE + '\n[apps.updater]\ntitle = "U"\npolicy = "floating"\ncontainers = ["u"]\n', "apps.updater: the key"),
            (self.BASE.replace('containers = ["dockge"]', 'containers = ["dockge", "sidecar"]'), "dockge runs Compose"),
            (self.BASE.replace('title = "Dockge"\npolicy = "floating"', 'title = "Dockge"\npolicy = "report"\nreleases = "github:a/b"'), "dockge runs Compose"),
            (self.BASE + app + 'policy = "report"\ncontainers = ["x"]\nreleases = "hub:nginx"\n', "hub:library/<name>"),
            (self.BASE + app + 'policy = "floating"\ncontainers = ["x"]\nimage = "traefik"\n', "with no digest"),
            (self.BASE + app + 'policy = "floating"\ncontainers = ["x"]\nimage = "traefik:v3@sha256:00"\n', "with no digest"),
            (self.BASE + app + 'policy = "report"\nreleases = "plex"\n' + '\n[apps.y]\ntitle = "Y"\npolicy = "report"\nreleases = "plex"\n', "apps.x and apps.y both follow plex"),
            (self.BASE + app + 'policy = "report"\ncontainers = ["x"]\nreleases = "qts"\n', "qts is the NAS firmware"),
            (self.BASE + app + 'policy = "floating"\nreleases = "qts"\n', "qts is the NAS firmware"),
            (self.BASE + app + 'policy = "report"\nreleases = "qts"\n' + '\n[apps.y]\ntitle = "Y"\npolicy = "report"\nreleases = "qts"\n', "apps.x and apps.y both follow qts"),
            (self.BASE + app + 'policy = "floating"\ncontainers = ["x"]\npin_minor = true\n', "pin_minor is for semver apps"),
            (self.BASE + app + 'policy = "semver"\ncontainers = ["x"]\nreleases = "github:a/b"\npin_minor = 1\n', "pin_minor must be true or false"),
            (self.BASE + app + 'policy = "floating"\ncontainers = ["x"]\nrollback = true\n', "a floating app always rolls back"),
            (self.BASE + app + 'policy = "semver"\ncontainers = ["x"]\nreleases = "github:a/b"\nrollback = "yes"\n', "rollback must be true or false"),
            (self.BASE + app + 'policy = "semver"\ncontainers = ["x"]\nreleases = "github:a/b"\nrollback_files = ["/a"]\n', "needs rollback = true"),
            (self.BASE + app + 'policy = "semver"\ncontainers = ["x"]\nreleases = "github:a/b"\nrollback = true\nrollback_files = ["a"]\n', "absolute paths"),
            (self.BASE + app + 'policy = "floating"\ncontainers = ["x"]\nhold_major = "yes"\n', "hold_major must be true or false"),
            (self.BASE + app + 'policy = "semver"\ncontainers = ["x"]\nreleases = "github:a/b"\nhold_major = true\n', "hold_major is for floating apps"),
        ]
        for text, message in cases:
            with self.subTest(message=message):
                self.rejects(text, message)

    def test_a_broken_config_is_mailed_and_fails_the_run(self):
        main = MainTests("test_only_the_weekly_run_prunes")
        main.setUp()
        self.addCleanup(main.doCleanups)
        broken = mock.patch.object(update, "load_config", side_effect=ConfigError("config.toml: apps.x.policy must be one of floating, semver, report"))
        with broken:
            self.assertEqual(update.main(["--dry-run"]), 1)
            self.assertEqual(update.main(["--check"]), 1)
            self.assertEqual(main.letters, [])
            self.assertEqual(update.main([]), 1)
            main.run.assert_not_called()
            self.assertEqual(main.subjects(), ["Container update failed"])
            self.assertIn("Nothing was updated", update.FAILURES.read_text())
            self.assertFalse(update.LOCK.exists())
            with mock.patch.dict(os.environ, {"UPDATE_TODAY": "2026-11-16", "UPDATE_WDAY": "1"}):
                self.assertEqual(update.main([]), 1)
            self.assertEqual(main.subjects(), [])

    def test_a_broken_config_under_a_held_lock_moves_the_weekly_mail(self):
        main = MainTests("test_only_the_weekly_run_prunes")
        main.setUp()
        self.addCleanup(main.doCleanups)
        update.LOCK.mkdir(parents=True)
        with mock.patch.object(update, "load_config", side_effect=ConfigError("config.toml: bad")):
            self.assertEqual(update.main([]), 1)
            self.assertFalse(update.DUE.is_file())
            self.assertEqual(update.main(["--digest"]), 1)
        self.assertEqual(main.letters, [])
        self.assertTrue(update.DUE.is_file())
        self.assertTrue(update.LOCK.exists())

    def test_image_is_compared_without_the_registry(self):
        loaded = self.load(self.BASE.replace('containers = ["traefik"]', 'containers = ["traefik"]\nimage = "docker.io/library/traefik:v3"'))
        self.assertEqual(loaded.apps["traefik"].image, "traefik:v3")


class ComposeLabelTests(unittest.TestCase):
    LABELS = {
        "com.docker.compose.project": "immich",
        "com.docker.compose.service": "immich-server",
        "com.docker.compose.project.config_files": "/s/immich/docker-compose.yml,/s/immich/docker-compose.override.yml",
        "com.docker.compose.project.working_dir": "/s/immich",
    }

    def test_labels_name_the_project_and_every_file(self):
        self.assertEqual(stack_of(self.LABELS), Stack("immich", ("/s/immich/docker-compose.yml", "/s/immich/docker-compose.override.yml"), "/s/immich"))
        self.assertIsNone(stack_of({"com.docker.compose.project": "immich"}))
        self.assertIsNone(stack_of({}))

    def test_inspect_reads_the_labels(self):
        from runtime import Docker

        payload = {
            "Config": {"Image": "ghcr.io/immich-app/immich-server:v3", "Labels": self.LABELS},
            "Image": "sha256:srv", "State": {"Status": "running"}, "RestartCount": 0,
        }
        docker = Docker()
        with mock.patch.object(docker, "_request", return_value=payload):
            info = docker.inspect("immich_server")
        self.assertEqual(info.service, "immich-server")
        self.assertEqual(info.stack, stack_of(self.LABELS))

    def test_image_version_reads_the_image_label(self):
        from runtime import Docker

        docker = Docker()
        payload = {"Id": "sha256:new", "Config": {"Labels": {"org.opencontainers.image.version": "7.0.3.10918-ls330", "build_version": "x"}}}
        with mock.patch.object(docker, "_request", return_value=payload) as request:
            self.assertEqual(docker.image_version("lscr.io/linuxserver/radarr:latest"), "7.0.3.10918-ls330")
        self.assertEqual(request.call_args.args, ("GET", "/images/lscr.io%2Flinuxserver%2Fradarr%3Alatest/json"))
        for bare in ({"Id": "sha256:x", "Config": {"Labels": None}}, {"Id": "sha256:x", "Config": {}}, {"Id": "sha256:x"}):
            with mock.patch.object(docker, "_request", return_value=bare):
                self.assertEqual(docker.image_version("x:1"), "")

    def test_compose_passes_every_file(self):
        from runtime import Docker

        docker = Docker(compose_container="dockge")
        with mock.patch.object(docker, "_exec", return_value=(0, "")) as exec_:
            docker.compose(stack_of(self.LABELS), "up", "-d", "immich-server")
        container, cmd = exec_.call_args.args
        self.assertEqual(container, "dockge")
        self.assertEqual(cmd, [
            "/usr/bin/docker", "compose", "--project-directory", "/s/immich", "-p", "immich",
            "-f", "/s/immich/docker-compose.yml", "-f", "/s/immich/docker-compose.override.yml",
            "up", "-d", "immich-server",
        ])
        with self.assertRaises(DockerError):
            Docker().compose(stack_of(self.LABELS), "up")

    def test_the_compose_container_is_recreated_with_every_file(self):
        from runtime import Docker

        docker = Docker()
        stack = Stack("dockge", ("/s/dockge/compose.yaml",), "/s/dockge")
        replies = iter([{}, {"Id": "one-shot"}, {}, {"StatusCode": 0}, {}])
        with mock.patch.object(docker, "_request", side_effect=lambda *_args: next(replies)) as request, \
                mock.patch.object(docker, "_read", return_value=b""):
            docker.recreate_runner("louislam/dockge:1", stack)
        create = request.call_args_list[1].args
        self.assertEqual(create[1], "/containers/create?name=updates-dockge-recreate")
        self.assertEqual(create[2]["Cmd"], [
            "compose", "--project-directory", "/s/dockge", "-p", "dockge", "-f", "/s/dockge/compose.yaml", "up", "-d", "--pull", "never",
        ])

    def test_immich_is_recreated_with_its_override(self):
        files = ("/stacks/immich/docker-compose.yml", "/stacks/immich/docker-compose.override.yml")
        test = ImmichTests("test_patch_pulls_the_exact_tag_and_retags_v3")
        test.setUp()
        self.addCleanup(test.doCleanups)
        for box in test.docker.boxes.values():
            box.files = files
        test.update()
        self.assertTrue(test.docker.stacks)
        self.assertEqual({stack.files for stack in test.docker.stacks}, {files})

    def test_a_container_compose_did_not_start_is_left_alone(self):
        docker = FakeDocker({"traefik": Box("traefik:v3", "old", "traefik", "traefik", labeled=False)}, {"traefik:v3": "new"})
        outcome = Outcome()
        bump_traefik(docker, outcome)
        self.assertEqual(docker.calls, [])
        self.assertIn("was not started by Compose", outcome.issues[0].detail)

    def test_a_group_split_over_projects_is_left_alone(self):
        docker = FakeDocker({
            "beszel": Box("henrygd/beszel:0", "hub", "beszel", "beszel"),
            "beszel-agent": Box("henrygd/beszel-agent:0", "agent", "agents", "beszel-agent"),
        }, {"henrygd/beszel:0": "hub-new"})
        outcome = Outcome()
        bump(docker, "beszel", outcome)
        self.assertEqual(docker.calls, [])
        self.assertIn("different Compose projects", outcome.issues[0].detail)

    def test_beszel_moves_together_and_rolls_back_together(self):
        docker = FakeDocker({
            "beszel": Box("henrygd/beszel:0", "hub", "beszel", "beszel"),
            "beszel-agent": Box("henrygd/beszel-agent:0", "agent", "beszel", "beszel-agent"),
        }, {"henrygd/beszel:0": "hub-new", "henrygd/beszel-agent:0": "agent-new"})
        docker.unhealthy = {"agent-new"}
        outcome = Outcome()
        bump(docker, "beszel", outcome)
        self.assertIn(("compose", "beszel", "up", "-d", "--pull", "never", "--no-deps", "beszel", "beszel-agent"), docker.calls)
        self.assertEqual((docker.boxes["beszel"].image_id, docker.boxes["beszel-agent"].image_id), ("hub", "agent"))
        self.assertEqual(outcome.rejected, [("beszel", "hub-new"), ("beszel-agent", "agent-new")])
        self.assertIn("The new images did not become healthy", outcome.issues[0].detail)

    def test_the_compose_container_goes_last_through_a_one_shot(self):
        docker = FakeDocker({
            "dockge": Box("louislam/dockge:1", "dockge-old", "dockge", "dockge"),
            "traefik": Box("traefik:v3", "old", "traefik", "traefik"),
        }, {"louislam/dockge:1": "dockge-new", "traefik:v3": "new"})
        with mock.patch.dict(os.environ, {"PLEX_VERSION": "", "QTS_VERSION": ""}), \
                mock.patch.object(jobs, "github_releases", return_value=[]):
            outcome = run(docker, CONFIG, False)
        self.assertEqual([bump.key for bump in outcome.bumps], ["traefik", "dockge"])
        ups = [call for call in docker.calls if call[0] in ("recreate",) or call[:3] == ("compose", "traefik", "up")]
        self.assertEqual(ups, [("compose", "traefik", "up", "-d", "--pull", "never", "--no-deps", "traefik"), ("recreate", "dockge")])


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


class OutsideAutoTests(AutoCase):
    """A floating release past its tag: asked about after a week, then reported (shadow) or moved to (on)."""

    def setUp(self):
        super().setUp()
        self.path = self.compose_file("traefik", TRAEFIK_COMPOSE)
        self.docker = FileDocker(
            {"traefik": Box("traefik:v3", "old", "traefik", "traefik", (str(self.path),))},
            {"traefik:v4": "new"},
            {"old": "3.6.2", "new": "4.0.1"},
        )
        self.releases = [noted("v3.6.2", 60, "Fixes."), noted("v4.0.0", 10, "## Changes\n- Faster routing."), noted("v4.0.1", 2, "- Fix a crash.")]
        self.use(auto="on")

    def use(self, **changes) -> None:
        settings = {"critical": False, "auto": "shadow", "linked_notes": False, "rollback_files": (), **changes}
        self.app = replace(APPS["traefik"], **settings)
        self.config = replace(CONFIG, apps={**APPS, "traefik": self.app})

    def collect(self, known=None, dry: bool = False, morning: bool = True, rejected=frozenset()) -> Outcome:
        self.outcome = Outcome()
        self.judged = self.judge(self.docker, known, asking=morning)
        today = jobs.Morning(self.docker, self.config, dry, self.outcome, rejected) if morning else None
        with mock.patch.object(jobs, "github_releases", return_value=self.releases):
            jobs.collect_outside(self.docker, self.config, set(self.docker.boxes), self.outcome.notices, self.outcome.unchecked, self.judged, today)
        return self.outcome

    def test_on_moves_the_tag_and_commits_the_file(self):
        outcome = self.collect()
        self.assertEqual(self.path.read_text(), TRAEFIK_COMPOSE.replace('"traefik:v3"', '"traefik:v4"'))
        self.assertEqual(self.docker.boxes["traefik"].image_id, "new")
        self.assertEqual(outcome.bumps, [Bump("traefik", "3.6.2", "4.0.1", TRAEFIK_URL, "Only fixes.")])
        self.assertEqual((outcome.notices, outcome.issues, outcome.rejected), ([], [], []))
        image, cmd, options = self.docker.ran[0]
        self.assertEqual(image, auto.TOOLS)
        self.assertEqual(cmd[-3:], ["traefik: traefik to v4\n\nTraefik 4.0.1 went in by itself. Only fixes.", "--", "traefik/docker-compose.yml"])
        self.assertIn("--no-verify", cmd)
        self.assertIn("commit.gpgsign=false", cmd)
        self.assertEqual((options["workdir"], options["binds"]), (str(self.dir), [f"{self.dir}:{self.dir}"]))
        self.assertEqual([item[:3] for item in self.judged.fresh], [("traefik", "4.0.1", Verdict("auto", ("Only fixes.",)))])
        self.assertRegex(self.judged.fresh[0][3], r"^[0-9a-f]{16}$")

    def test_what_workers_ai_is_shown(self):
        self.collect()
        question = self.asked[0]
        self.assertEqual((question.title, question.running, question.available), ("Traefik", "3.6.2", "4.0.1"))
        self.assertEqual(question.images, "traefik traefik:v3 → traefik:v4")
        self.assertIn("--api.dashboard=true", question.compose)
        self.assertIn("## v4.0.0\n## Changes\n- Faster routing.", question.notes)
        self.assertIn("## v4.0.1\n- Fix a crash.", question.notes)
        self.assertNotIn("v3.6.2", question.notes)
        user = verdict.messages(question)[1]["content"]
        self.assertIn("<notes>", user)
        self.assertIn("values from .env are not shown", user)
        self.assertNotIn("moved to by hand", question.compose)
        self.assertIn("It saves no files first, so data the new version changes stays changed.", user)

    def test_workers_ai_is_told_which_files_the_updater_saves(self):
        self.use(rollback_files=("/acme/acme.json",))
        self.collect()
        user = verdict.messages(self.asked[0])[1]["content"]
        self.assertIn("stops the app and saves /acme/acme.json, starts the new version and waits for it to be healthy. If it is not, the updater puts those files back", user)
        self.assertIn("never a step for the owner", verdict.messages(self.asked[0])[0]["content"])

    def test_shadow_only_says_so(self):
        self.use(auto="shadow")
        outcome = self.collect()
        self.assertEqual(self.path.read_text(), TRAEFIK_COMPOSE)
        self.assertFalse([call for call in self.docker.calls if call[0] in ("compose", "config", "pull")])
        self.assertEqual(outcome.notices, [Notice("traefik", "3.6.2", "4.0.1", TRAEFIK_URL, True, "shadow", ("Only fixes.",))])
        self.assertEqual(outcome.bumps, [])

    def test_a_release_out_today_is_asked_about_that_morning(self):
        self.releases[1] = noted("v4.0.0", 0, "## Changes\n- Faster routing.")
        self.releases[2] = noted("v4.0.1", 0, "- Fix a crash.")
        self.collect()
        self.assertEqual(len(self.asked), 1)

    def test_a_compose_file_with_changes_not_committed_stays_by_hand(self):
        self.docker.status = " M traefik/docker-compose.yml\n"
        outcome = self.collect()
        self.assertEqual(self.asked, [])
        self.assertEqual(self.path.read_text(), TRAEFIK_COMPOSE)
        self.assertEqual(outcome.notices[0].verdict, "manual")
        self.assertIn("traefik/docker-compose.yml has changes that are not committed", outcome.notices[0].reasons[0])
        self.assertEqual(self.docker.statuses[0][-2:], ["--", "traefik/docker-compose.yml"])
        self.docker.status = ""
        self.collect()
        self.assertEqual(self.docker.boxes["traefik"].image_id, "new")

    def test_the_rollback_copies_are_private(self):
        self.docker.archives[("traefik", "/acme/acme.json")] = b"keys"
        saved = auto.save(self.docker, replace(self.app, rollback_files=("/acme/acme.json",)), "traefik")
        folder = auto.ROLLBACK / "traefik"
        self.assertEqual([stat.S_IMODE(path.stat().st_mode) for path in (auto.ROLLBACK, folder, folder / "paths.tsv")], [0o700, 0o700, 0o600])
        self.assertEqual(saved.files, (("/acme/acme.json", folder / "0.tar"),))

    def test_a_copy_out_of_a_container_is_private(self):
        class Reply(io.BytesIO):
            status = 200

        connection = mock.MagicMock()
        connection.getresponse.return_value = Reply(b"tar")
        target = self.dir / "copy.tar"
        docker = runtime.Docker()
        docker._prefix = "/v1.43"
        with mock.patch.object(runtime, "_UnixConnection", return_value=connection):
            self.assertTrue(docker.archive_to("traefik", "/acme/acme.json", target))
        self.assertEqual((target.read_bytes(), stat.S_IMODE(target.stat().st_mode)), (b"tar", 0o600))

    def test_with_wait_days_it_is_asked_once_its_line_is_old_enough(self):
        self.use(auto="on", wait_days=7)
        self.releases[1] = noted("v4.0.0", 3, "## Changes\n- Faster routing.")
        outcome = self.collect()
        self.assertEqual(self.asked, [])
        self.assertEqual(outcome.notices, [Notice("traefik", "3.6.2", "4.0.1", TRAEFIK_URL, False, "wait")])
        self.assertEqual(self.path.read_text(), TRAEFIK_COMPOSE)
        self.releases[1] = noted("v4.0.0", 7, "## Changes\n- Faster routing.")
        self.collect()
        self.assertEqual(len(self.asked), 1)

    def test_steps_to_take_keep_it_by_hand(self):
        self.answer = Verdict("auto", ("The dashboard moved.",), ("Rename the dashboard flag.",))
        outcome = self.collect()
        self.assertEqual(self.path.read_text(), TRAEFIK_COMPOSE)
        self.assertEqual(outcome.notices, [Notice("traefik", "3.6.2", "4.0.1", TRAEFIK_URL, True, "manual", ("The dashboard moved.",), ("Rename the dashboard flag.",), held=HELD_AI)])

    def test_a_flag_the_compose_file_sets_vetoes_an_auto(self):
        self.releases[1] = noted("v4.0.0", 10, "## Breaking changes\n- `--api.dashboard` is removed.")
        outcome = self.collect()
        self.assertEqual(self.asked, [])
        self.assertEqual(outcome.notices[0].verdict, "manual")
        self.assertEqual(outcome.notices[0].reasons, ("They name --api.dashboard, set in its compose file.",))
        self.assertEqual(outcome.notices[0].held, HELD_BREAKING)
        self.assertEqual(self.path.read_text(), TRAEFIK_COMPOSE)

    def test_a_change_that_cannot_be_undone_vetoes_an_auto(self):
        self.releases[1] = noted("v4.0.0", 10, "The store is migrated. Downgrading is not supported.")
        notice = self.collect().notices[0]
        self.assertEqual(notice.verdict, "manual")
        self.assertIn("Downgrading is not supported", notice.reasons[0])

    def test_each_question_and_its_answer_go_in_the_journal(self):
        self.answer = replace(Verdict("auto", ("Only fixes.",)), said='{"decision": "auto"}')
        self.collect()
        entry = self.journal()[0]
        self.assertEqual((entry["app"], entry["running"], entry["version"], entry["model"], entry["asked"]), ("traefik", "3.6.2", "4.0.1", verdict.MODEL, True))
        self.assertEqual(entry["messages"], verdict.messages(self.asked[0]))
        self.assertEqual(entry["answer"], '{"decision": "auto"}')
        self.assertEqual(entry["decision"], {"decision": "auto", "reasons": ["Only fixes."], "actions": []})
        self.assertEqual(oct((self.dir / "journal").stat().st_mode & 0o777), "0o700")
        self.assertEqual(oct(next((self.dir / "journal").glob("*.jsonl")).stat().st_mode & 0o777), "0o600")
        self.collect(known=self.judged.known)
        self.assertEqual(len(self.journal()), 1)

    def test_no_tokens_sends_it_by_hand_at_once_and_asks_nothing_more(self):
        self.answer = verdict.Unavailable("it has no tokens left today (HTTP 429)")
        outcome = self.collect()
        said = "Workers AI was not asked: it has no tokens left today (HTTP 429)."
        self.assertEqual(outcome.notices, [Notice("traefik", "3.6.2", "4.0.1", TRAEFIK_URL, True, "manual", (said,), unanswered=True)])
        self.assertEqual((self.judged.unavailable, self.judged.known, self.judged.failures), (said, {}, {}))
        self.assertEqual(self.journal()[0]["error"], "it has no tokens left today (HTTP 429)")
        self.assertIn(said, push.message(outcome.notices[0], "t")["message"])
        app = self.config.apps["traefik"]
        info = self.docker.inspect("traefik")
        self.assertIsNone(self.judged.verdict(app, info, info.stack, (3, 6, 2), self.releases[-1], self.releases, {"traefik": "traefik:v4"}, "4.0.1"))
        self.assertEqual(len(self.asked), 1)

    def test_with_no_cloudflare_settings_it_goes_out_by_hand_saying_so(self):
        said = "Workers AI was not asked: CLOUDFLARE_ACCOUNT_ID or CLOUDFLARE_AI_TOKEN is missing in .env."
        judge = auto.Judge(self.docker, {}, {}, True, unavailable=said)
        notices = []
        with mock.patch.object(jobs, "github_releases", return_value=self.releases):
            jobs.collect_outside(self.docker, self.config, set(self.docker.boxes), notices, [], judge, None)
        self.assertEqual(notices, [Notice("traefik", "3.6.2", "4.0.1", TRAEFIK_URL, True, "manual", (said,), unanswered=True)])
        self.assertEqual(self.asked, [])

    def test_no_answer_waits_unpushed_then_goes_out_by_hand(self):
        self.answer = VerdictError("Workers AI could not be reached")
        outcome = self.collect()
        self.assertEqual(outcome.notices, [Notice("traefik", "3.6.2", "4.0.1", TRAEFIK_URL, False, "pending")])
        self.assertEqual((self.judged.known, self.judged.fresh), ({}, []))
        first = date.today()
        self.assertEqual(self.judged.failures, {("traefik", "4.0.1"): first})
        later = auto.Judge(self.docker, {}, {}, True, None, {("traefik", "4.0.1"): first - timedelta(days=auto.RETRY_DAYS)})
        self.assertFalse(later.waiting("traefik", "4.0.1"))
        self.assertTrue(auto.Judge(self.docker, {}, {}, True, None, {("traefik", "4.0.1"): first}).waiting("traefik", "4.0.1"))
        self.answer = Verdict("manual", ("A flag was renamed.",))
        self.collect()
        self.assertEqual(self.judged.failures, {})

    def test_the_sunday_mail_holds_a_release_it_has_no_answer_for(self):
        notice = self.collect(morning=False).notices[0]
        self.assertEqual((notice.verdict, notice.by_hand, self.asked), ("pending", False, []))

    def test_an_answer_is_asked_again_once_what_it_was_given_for_changed(self):
        self.use(auto="shadow")
        self.collect()
        self.assertEqual(len(self.asked), 1)
        key = ("traefik", "4.0.1")
        known, prints = {key: self.judged.known[key]}, {key: self.judged.prints[key]}
        judge = auto.Judge(self.docker, {}, known, True, prints)
        self.assertEqual(judge.verdict(self.app, self.docker.inspect("traefik"), self.docker.inspect("traefik").stack, (3, 6, 2), self.releases[2], self.releases, {"traefik": "traefik:v4"}, "4.0.1"), known[key])
        self.assertEqual(len(self.asked), 1)
        self.releases[2] = noted("v4.0.1", 2, "- Fix a crash.\n- The dashboard flag is renamed.")
        judge.verdict(self.app, self.docker.inspect("traefik"), self.docker.inspect("traefik").stack, (3, 6, 2), self.releases[2], self.releases, {"traefik": "traefik:v4"}, "4.0.1")
        self.assertEqual(len(self.asked), 2)

    def test_a_rollback_holds_whatever_is_shown_later(self):
        judge = auto.Judge(self.docker, {}, {})
        judge.overrule("traefik", "4.0.1", "Rolled back.")
        self.assertEqual(judge.prints[("traefik", "4.0.1")], "")
        self.assertEqual(self.collect(known=judge.known).notices[0].reasons, ("Rolled back.",))
        self.assertEqual(self.asked, [])

    def test_a_verdict_is_asked_once(self):
        outcome = self.collect(known={("traefik", "4.0.1"): Verdict("manual", ("Said before.",))})
        self.assertEqual(self.asked, [])
        self.assertEqual(outcome.notices[0].reasons, ("Said before.",))

    def test_notes_that_may_miss_releases_are_manual_unasked(self):
        self.releases = self.releases[1:]
        notice = self.collect().notices[0]
        self.assertEqual(self.asked, [])
        self.assertEqual(notice.verdict, "manual")
        self.assertIn("stop after 3.6.2", notice.reasons[0])
        self.assertEqual(len(self.judged.fresh), 1)

    def test_an_unhealthy_release_is_rolled_back_and_stays_by_hand(self):
        self.docker.unhealthy = {"new"}
        outcome = self.collect()
        self.assertEqual(self.path.read_text(), TRAEFIK_COMPOSE)
        self.assertEqual(self.docker.boxes["traefik"].image_id, "old")
        self.assertIn("Traefik 4.0.1 did not become healthy. The previous image was restored.", outcome.issues[0].detail)
        self.assertEqual((outcome.rejected, outcome.bumps, self.docker.ran), ([("traefik", "4.0.1")], [], []))
        self.assertEqual(self.judged.known[("traefik", "4.0.1")].decision, "manual")
        self.assertEqual(self.collect(rejected={("traefik", "4.0.1")}).notices[0].verdict, "manual")

    def test_an_edit_that_changes_more_than_the_image_is_put_back(self):
        def drift(count, resolved):
            if count == 2:
                resolved["services"]["traefik"]["rest"] += "x"
            return resolved

        self.docker.config_hook = drift
        outcome = self.collect()
        self.assertEqual(self.path.read_text(), TRAEFIK_COMPOSE)
        self.assertIn("the edit changed more than the image", outcome.issues[0].detail)
        self.assertFalse([call for call in self.docker.calls if call[0] == "compose"])
        self.assertEqual(outcome.rejected, [])

    def test_a_failed_pull_puts_the_file_back_and_tries_again(self):
        self.docker.remote = {}
        outcome = self.collect()
        self.assertEqual(self.path.read_text(), TRAEFIK_COMPOSE)
        self.assertIn("the pull failed", outcome.issues[0].detail)
        self.assertIn("tried again the next morning", outcome.issues[0].detail)
        self.assertEqual(self.judged.known[("traefik", "4.0.1")].decision, "auto")

    @unittest.skipIf(os.getuid() == 0, "root writes read-only files")
    def test_a_file_the_updater_cannot_write_stays_by_hand_unasked(self):
        self.path.chmod(0o444)
        self.addCleanup(self.path.chmod, 0o644)
        notice = self.collect().notices[0]
        self.assertEqual(self.asked, [])
        self.assertEqual(notice.reasons, ("It can't go in by itself: the updater can't write docker-compose.yml.",))

    def test_an_image_from_a_variable_stays_by_hand_unasked(self):
        self.path.write_text(TRAEFIK_COMPOSE.replace('"traefik:v3"', "traefik:${TRAEFIK_TAG}"))
        notice = self.collect().notices[0]
        self.assertEqual(self.asked, [])
        self.assertIn("not a plain image:tag", notice.reasons[0])

    def test_the_sunday_mail_shows_what_the_morning_will_do(self):
        outcome = self.collect(known={("traefik", "4.0.1"): Verdict("auto", ("Only fixes.",))}, morning=False)
        self.assertEqual(outcome.notices, [Notice("traefik", "3.6.2", "4.0.1", TRAEFIK_URL, False, "auto", ("Only fixes.",))])
        self.assertEqual(self.docker.calls, [])

    def test_a_dry_run_asks_and_changes_nothing(self):
        outcome = self.collect(dry=True)
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(self.path.read_text(), TRAEFIK_COMPOSE)
        self.assertEqual(outcome.notices[0].verdict, "auto")
        self.assertFalse([call for call in self.docker.calls if call[0] in ("compose", "pull")])

    def test_a_commit_that_fails_is_mailed_and_the_update_stays(self):
        self.docker.run_code = 1
        outcome = self.collect()
        self.assertEqual(self.docker.boxes["traefik"].image_id, "new")
        self.assertEqual(outcome.issues, [Issue("traefik", "4.0.1 went in by itself, but the compose file change was not committed: fatal: boom")])
        self.assertEqual(len(outcome.bumps), 1)

    def test_a_critical_app_keeps_majors_unasked(self):
        self.use(auto="on", critical=True)
        outcome = self.collect()
        self.assertEqual(self.asked, [])
        self.assertEqual(outcome.notices, [Notice("traefik", "3.6.2", "4.0.1", TRAEFIK_URL)])

    def test_without_a_judge_nothing_changes(self):
        outcome = Outcome()
        with mock.patch.object(jobs, "github_releases", return_value=self.releases):
            jobs.collect_outside(self.docker, self.config, set(self.docker.boxes), outcome.notices)
        self.assertEqual(outcome.notices, [Notice("traefik", "3.6.2", "4.0.1", TRAEFIK_URL)])
        self.assertEqual(self.asked, [])


class HomeAssistantAutoTests(AutoCase):
    """The inventory and linked notes: a breaking change in an integration set up stays by hand."""

    BLOG = "https://www.home-assistant.io/blog/2026/12/03/release-202612/"

    def setUp(self):
        super().setUp()
        path = self.compose_file("homeassistant", "services:\n  homeassistant:\n    image: ghcr.io/home-assistant/home-assistant:2026.11\n")
        image = "ghcr.io/home-assistant/home-assistant:2026.11"
        self.docker = FileDocker(
            {"homeassistant": Box(image, "old", "homeassistant", "homeassistant", (str(path),))},
            {"ghcr.io/home-assistant/home-assistant:2026.12": "new"},
            {"old": "2026.11.3", "new": "2026.12.1"},
        )
        entries = {"data": {"entries": [{"domain": "mqtt", "title": "Broker at home"}, {"domain": "zha", "title": "Zigbee"}]}}
        self.docker.paths[("homeassistant", "/config/.storage/core.config_entries")] = json.dumps(entries).encode()
        self.docker.paths[("homeassistant", "/config/configuration.yaml")] = b"default_config:\n"
        self.docker.paths[("homeassistant", "/config/automations.yaml")] = b"- triggers:\n  - trigger: sun\n"
        self.docker.paths[("homeassistant", "/config/scripts.yaml")] = b""
        self.releases = [noted("2026.11.3", 40), noted("2026.12.0", 9, f"Read the [release notes]({self.BLOG})."), noted("2026.12.1", 2)]
        self.app = replace(APPS["homeassistant"], auto="on", rollback_files=())
        self.config = replace(CONFIG, apps={**APPS, "homeassistant": self.app})

    def collect(self, page: str) -> Outcome:
        outcome = Outcome()
        judge = self.judge(self.docker)
        with mock.patch.object(jobs, "github_releases", return_value=self.releases), \
                mock.patch.object(notes, "fetch_page", return_value=page) as self.fetched:
            jobs.collect_outside(self.docker, self.config, set(self.docker.boxes), outcome.notices, None, judge, jobs.Morning(self.docker, self.config, False, outcome))
        return outcome

    def test_a_breaking_change_in_an_integration_set_up_stays_by_hand(self):
        outcome = self.collect("## Backward-incompatible changes {#bic}\n**MQTT**\nThe broker option moved (/integrations/mqtt/)")
        self.fetched.assert_called_once_with(self.BLOG)
        self.assertEqual(outcome.notices[0].verdict, "manual")
        self.assertEqual(outcome.notices[0].reasons[0], "They name mqtt, which this setup uses.")
        self.assertEqual(self.asked, [])
        entry = self.journal()[0]
        self.assertEqual((entry["asked"], entry["decision"]["decision"]), (False, "manual"))
        shown = entry["messages"][1]["content"]
        self.assertIn(f"From {self.BLOG}, linked from these notes:", shown)
        self.assertIn("In use by this setup, every domain in /config/.storage/core.config_entries: mqtt, zha", shown)
        self.assertNotIn("Broker at home", shown)
        self.assertIn("File /config/automations.yaml:\n```\n- triggers:\n  - trigger: sun\n```", shown)
        self.assertIn("File /config/scripts.yaml:", shown)
        self.assertEqual(outcome.bumps, [])

    def test_a_beta_is_shown_but_its_links_are_not_read(self):
        self.releases.insert(1, replace(noted("2026.12.0", 12, "See the [release notes](https://rc.a.test/latest)."), tag="2026.12.0b0", prerelease=True))
        self.collect("## Backward-incompatible changes\nNothing here.")
        self.fetched.assert_called_once_with(self.BLOG)
        self.assertIn("## 2026.12.0b0\nSee the [release notes](https://rc.a.test/latest).\n\n## 2026.12.0", self.asked[0].notes)

    def test_a_long_linked_page_is_cut_and_stays_by_hand(self):
        outcome = self.collect("## Breaking changes\n" + "- A comment about it.\n" * 4000)
        self.assertEqual(self.asked, [])
        self.assertIn(f"[cut at {auto.MAX_PAGE} characters]", self.journal()[0]["messages"][1]["content"])
        self.assertEqual(outcome.notices[0].verdict, "manual")
        self.assertEqual(outcome.notices[0].reasons[0], "A page the notes link was too long to show whole, so a step in it may be missed.")

    def test_an_inventory_kept_one_object_per_line(self):
        lines = b'{"type":"Coordinator"}\n{"modelId":"ZBMINIR2","ieeeAddr":"0x1"}\n{"modelId":"TS130F"}\n{"modelId":"ZBMINIR2"}\n'
        self.docker.paths[("homeassistant", "/app/data/database.db")] = lines
        app = replace(self.app, inventory_file="/app/data/database.db", inventory_key="modelId")
        self.assertEqual(auto.inventory(self.docker, app, self.docker.inspect("homeassistant")), ["TS130F", "ZBMINIR2"])

    def test_a_linked_page_that_is_gone_is_shown_as_missing(self):
        gone = urllib.error.HTTPError(self.BLOG, 404, "Not Found", {}, None)
        outcome = Outcome()
        with mock.patch.object(jobs, "github_releases", return_value=self.releases), mock.patch.object(notes, "fetch_page", side_effect=gone):
            jobs.collect_outside(self.docker, self.config, set(self.docker.boxes), outcome.notices, None, self.judge(self.docker), None)
        self.assertIn("(could not be read: HTTP 404)", self.asked[0].notes)

    def test_a_month_that_touches_nothing_set_up_moves_the_tag(self):
        outcome = self.collect("## Backward-incompatible changes\n**Plex**\nA sensor was renamed (/integrations/plex/)")
        self.assertEqual([(bump.before, bump.after) for bump in outcome.bumps], [("2026.11.3", "2026.12.1")])
        self.assertIn("home-assistant:2026.12\n", (self.dir / "homeassistant" / "docker-compose.yml").read_text())

    def test_an_exact_pin_asks_about_a_patch_and_moves_to_its_exact_tag(self):
        image = "ghcr.io/home-assistant/home-assistant:2026.11.3"
        path = self.compose_file("homeassistant", f"services:\n  homeassistant:\n    image: {image}\n")
        paths = self.docker.paths
        self.docker = FileDocker(
            {"homeassistant": Box(image, "old", "homeassistant", "homeassistant", (str(path),))},
            {"ghcr.io/home-assistant/home-assistant:2026.11.4": "new"},
            {"old": "2026.11.3", "new": "2026.11.4"},
        )
        self.docker.paths.update(paths)
        self.releases = [noted("2026.11.3", 40), noted("2026.11.4", 2, "- Fix a thing.")]
        outcome = self.collect("")
        self.assertEqual(len(self.asked), 1)
        self.assertEqual([(bump.before, bump.after) for bump in outcome.bumps], [("2026.11.3", "2026.11.4")])
        self.assertIn("home-assistant:2026.11.4\n", path.read_text())


class HomeAssistantPatchTests(AutoCase):
    """A patch the 2026.11 tag takes by itself: asked about only when its notes mark a breaking change to something set up."""

    IMAGE = "ghcr.io/home-assistant/home-assistant:2026.11"

    def setUp(self):
        super().setUp()
        path = self.compose_file("homeassistant", f"services:\n  homeassistant:\n    image: {self.IMAGE}\n")
        self.docker = FileDocker(
            {"homeassistant": Box(self.IMAGE, "old", "homeassistant", "homeassistant", (str(path),))},
            {self.IMAGE: "new"},
            {"old": "2026.11.3", "new": "2026.11.4"},
        )
        entries = {"data": {"entries": [{"domain": "mqtt"}, {"domain": "zha"}]}}
        self.docker.paths[("homeassistant", "/config/.storage/core.config_entries")] = json.dumps(entries).encode()
        for name in ("configuration", "automations", "scripts"):
            self.docker.paths[("homeassistant", f"/config/{name}.yaml")] = b""
        self.app = replace(APPS["homeassistant"], auto="on", linked_notes=False, rollback_files=(), breaking_marker="(breaking-change)", breaking_names="[{} docs]")

    def update(self, line: str, judge: auto.Judge | None = None) -> Outcome:
        outcome = Outcome()
        self.judged = judge or self.judge(self.docker)
        releases = [noted("2026.11.3", 10), noted("2026.11.4", 1, f"- Fix a thing ([@a] - [#1])\n{line}")]
        with mock.patch.object(jobs, "github_releases", return_value=releases):
            update_floating(self.docker, CONFIG, set(self.docker.boxes), False, outcome, frozenset(), self.app, judge=self.judged)
        return outcome

    def test_a_patch_with_no_breaking_change_for_this_setup_goes_in_unasked(self):
        for line in ("- Speed up startup ([@a] - [#2]) ([zha docs])", "- Remove polling ([@a] - [#3]) ([omnilogic docs]) (breaking-change)"):
            with self.subTest(line=line):
                self.docker.boxes["homeassistant"] = replace(self.docker.boxes["homeassistant"], image_id="old")
                self.docker.images[self.IMAGE] = "new"
                outcome = self.update(line)
                self.assertEqual(self.docker.boxes["homeassistant"].image_id, "new")
                self.assertEqual((self.asked, outcome.notices), ([], []))

    def test_a_breaking_change_to_an_integration_set_up_is_asked_about(self):
        outcome = self.update("- Rename a ZHA sensor ([@a] - [#4]) ([zha docs]) (breaking-change)")
        self.assertEqual(len(self.asked), 1)
        self.assertIn("Rename a ZHA sensor", self.asked[0].notes)
        self.assertEqual(self.docker.boxes["homeassistant"].image_id, "new")
        self.assertEqual(outcome.bumps[0].reason, "Only fixes.")

    def test_short_notes_with_their_link_definitions_are_asked_about_too(self):
        outcome = self.update("- Rename a ZHA sensor ([@a] - [#4]) ([zha docs]) (breaking-change)\n\n"
                              "[zha docs]: https://www.home-assistant.io/integrations/zha/\n[mqtt docs]: https://www.home-assistant.io/integrations/mqtt/")
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(outcome.bumps[0].reason, "Only fixes.")

    def test_a_breaking_change_to_home_assistant_itself_is_asked_about_and_can_stay_by_hand(self):
        self.answer = Verdict("manual", ("Scripts that loop forever now stop.",))
        outcome = self.update("- Stop scripts that loop more than 10000 times ([@a] - [#5]) (breaking-change)")
        self.assertEqual(len(self.asked), 1)
        self.assertEqual((self.docker.boxes["homeassistant"].image_id, self.docker.images[self.IMAGE]), ("old", "old"))
        notice = outcome.notices[0]
        self.assertEqual((notice.running, notice.available, notice.verdict), ("2026.11.3", "2026.11.4", "manual"))
        self.assertEqual(notice.reasons, (
            "“Stop scripts that loop more than 10000 times ([@a] - [#5]) (breaking-change)”",
            "Scripts that loop forever now stop.",
        ))
        self.assertEqual(notice.held, HELD_AI)

    def test_with_no_tokens_a_marked_patch_goes_out_by_hand_saying_both(self):
        said = "Workers AI was not asked: it has no tokens left today (HTTP 429)."
        outcome = self.update("- Change ZHA groups ([@a] - [#6]) ([zha docs]) (breaking-change)", auto.Judge(self.docker, {}, {}, True, unavailable=said))
        self.assertEqual(self.asked, [])
        self.assertEqual(self.docker.boxes["homeassistant"].image_id, "old")
        self.assertEqual(outcome.notices[0].reasons, ("“Change ZHA groups ([@a] - [#6]) ([zha docs]) (breaking-change)”", said))
        self.assertEqual(outcome.notices[0].held, HELD_BREAKING)

    def test_notes_that_cannot_be_read_hold_the_patch_until_the_next_morning(self):
        outcome = Outcome()
        with mock.patch.object(jobs, "github_releases", side_effect=OSError("GitHub is down")):
            update_floating(self.docker, CONFIG, set(self.docker.boxes), False, outcome, frozenset(), self.app, judge=self.judge(self.docker))
        self.assertEqual(self.docker.boxes["homeassistant"].image_id, "old")
        self.assertEqual((outcome.notices, outcome.unchecked), ([], [("homeassistant", "GitHub is down")]))

    def test_a_rebuild_of_the_version_running_goes_in_unasked(self):
        self.docker.versions["new"] = "2026.11.3"
        outcome = self.update("- Rename a ZHA sensor ([@a] - [#4]) ([zha docs]) (breaking-change)")
        self.assertEqual(self.docker.boxes["homeassistant"].image_id, "new")
        self.assertEqual((self.asked, outcome.unchecked), ([], []))

    def test_a_check_that_fails_puts_the_tag_back_on_the_running_image(self):
        outcome = Outcome()
        with mock.patch.object(jobs, "github_releases", return_value=[noted("2026.11.3", 10), noted("2026.11.4", 1, "- X (breaking-change)")]), \
                mock.patch.object(jobs, "_decide", side_effect=LookupError("unknown charset")):
            update_floating(self.docker, CONFIG, set(self.docker.boxes), False, outcome, frozenset(), self.app, judge=self.judge(self.docker))
        self.assertEqual((self.docker.boxes["homeassistant"].image_id, self.docker.images[self.IMAGE]), ("old", "old"))
        self.assertEqual(outcome.unchecked, [("homeassistant", "unknown charset")])

    def test_marked_lines(self):
        text = "- A ([zha docs]) (breaking-change)\n- B ([plex docs]) (breaking-change)\n- C (breaking-change)\n- D ([zha docs])"
        self.assertEqual(notes.marked(text, "(breaking-change)", "[{} docs]", ["zha"]), ["A ([zha docs]) (breaking-change)", "C (breaking-change)"])
        self.assertEqual(len(notes.marked(text, "(breaking-change)", "", [])), 3)

    def test_explain_says_patches_with_a_marked_change_are_asked_about(self):
        self.assertEqual(jobs.explain(self.app, "2026.11")[0], ("patch", "by itself, unless its notes mark a breaking change to something in use; then Workers AI is asked: by itself if nothing needs doing by hand, else emailed"))


class HoldMajorAutoTests(AutoCase):
    """A major hold_major keeps back, installed with its database saved first."""

    IMAGE = "lscr.io/linuxserver/radarr:latest"
    DB, WAL = "/config/radarr.db", "/config/radarr.db-wal"

    def setUp(self):
        super().setUp()
        path = self.compose_file("media-automation", f"services:\n  radarr:\n    image: {self.IMAGE}\n")
        self.docker = FileDocker(
            {"radarr": Box(self.IMAGE, "old", "media-automation", "radarr", (str(path),))},
            {self.IMAGE: "new"},
            {"old": "6.4.4.10685-ls325", "new": "7.0.3.10918-ls330"},
        )
        self.docker.archives[("radarr", self.DB)] = b"v6 db"
        self.releases = [noted("v6.4.4.10685", 60), noted("v7.0.0.10900", 10, "- New UI."), noted("v7.0.3.10918", 1)]
        self.app = replace(APPS["radarr"], auto="on", rollback_files=(self.DB, self.WAL))

    def update(self) -> Outcome:
        outcome = Outcome()
        self.judged = self.judge(self.docker)
        with mock.patch.object(jobs, "github_releases", return_value=self.releases):
            update_floating(self.docker, CONFIG, set(self.docker.boxes), False, outcome, frozenset(), self.app, judge=self.judged)
        return outcome

    def test_the_major_goes_in_with_its_database_saved_while_stopped(self):
        outcome = self.update()
        self.assertEqual(self.docker.boxes["radarr"].image_id, "new")
        self.assertEqual(outcome.bumps, [Bump("radarr", "6.4.4.10685", "7.0.3.10918", "https://github.com/Radarr/Radarr/releases/tag/v7.0.3.10918", "Only fixes.")])
        steps = [call[:4] for call in self.docker.calls if call[0] in ("compose", "archive_to")]
        self.assertEqual(steps[1:4], [("compose", "media-automation", "stop", "radarr"), ("archive_to", "radarr", self.DB), ("archive_to", "radarr", self.WAL)])
        saved = self.dir / "rollback" / "radarr"
        self.assertEqual((saved / "0.tar").read_bytes(), b"v6 db")
        self.assertEqual((saved / "paths.tsv").read_text(), f"0.tar\t{self.DB}\n1.tar\t{self.WAL}\n")
        self.assertEqual(self.docker.ran, [])
        self.assertEqual(outcome.notices, [])

    def test_a_major_whose_releases_cant_be_read_is_still_mailed_by_hand(self):
        outcome = Outcome()
        with mock.patch.object(jobs, "github_releases", side_effect=OSError("rate limited")):
            update_floating(self.docker, CONFIG, set(self.docker.boxes), False, outcome, frozenset(), self.app, judge=self.judge(self.docker))
        self.assertEqual(self.docker.boxes["radarr"].image_id, "old")
        self.assertEqual([(notice.key, notice.available, notice.by_hand) for notice in outcome.notices], [("radarr", "7.0.3.10918", True)])

    def test_an_unhealthy_major_gets_its_files_back_exactly(self):
        self.docker.unhealthy = {"new"}

        def migrate(stack, *args):
            original(stack, *args)
            if args[:2] == ("up", "-d") and self.docker.boxes["radarr"].image_id == "new":
                self.docker.archives.update({("radarr", self.DB): b"v7 db", ("radarr", self.WAL): b"v7 wal"})

        original, self.docker.compose = self.docker.compose, migrate
        outcome = self.update()
        self.assertEqual(self.docker.boxes["radarr"].image_id, "old")
        self.assertEqual(self.docker.images[self.IMAGE], "old")
        self.assertEqual(self.docker.archives[("radarr", self.DB)], b"v6 db")
        self.assertNotIn(("radarr", self.WAL), self.docker.archives)
        image, cmd, options = self.docker.ran[0]
        self.assertEqual((image, cmd), (auto.TOOLS, ["-rf", "--", self.DB, self.WAL]))
        self.assertEqual((options["entrypoint"], options["user"], options["volumes_from"]), (["rm"], "0:0", ["radarr"]))
        self.assertEqual(outcome.rejected, [("radarr", "new")])
        self.assertEqual(self.judged.known[("radarr", "7.0.3.10918")].decision, "manual")

    def test_a_stop_signal_midway_puts_the_previous_version_back(self):
        calls = []
        original = self.docker.wait_ready

        def wait_ready(name):
            calls.append(name)
            if len(calls) == 1:
                raise SystemExit(143)
            return original(name)

        self.docker.wait_ready = wait_ready
        with self.assertRaises(SystemExit):
            self.update()
        self.assertEqual((self.docker.boxes["radarr"].image_id, self.docker.images[self.IMAGE]), ("old", "old"))
        self.assertEqual(self.docker.archives[("radarr", self.DB)], b"v6 db")

    def test_no_rollback_tool_means_nothing_is_stopped(self):
        self.docker.unpullable = {auto.TOOLS}
        outcome = self.update()
        self.assertEqual((self.docker.boxes["radarr"].image_id, self.docker.images[self.IMAGE]), ("old", "old"))
        self.assertNotIn(("compose", "media-automation", "stop", "radarr"), [call[:4] for call in self.docker.calls])
        self.assertIn("the image a rollback needs could not be pulled", outcome.issues[0].detail)

    def test_shadow_keeps_the_hold(self):
        self.app = replace(self.app, auto="shadow")
        outcome = self.update()
        self.assertEqual((self.docker.boxes["radarr"].image_id, self.docker.images[self.IMAGE]), ("old", "old"))
        self.assertEqual(outcome.notices[0].verdict, "shadow")
        self.assertFalse([call for call in self.docker.calls if call[0] == "archive_to"])

    def test_a_backup_that_fails_starts_the_old_container_again(self):
        def broken(name, path, target):
            raise DockerError("disk full")

        self.docker.archive_to = broken
        outcome = self.update()
        self.assertIn("radarr could not be backed up first: disk full", outcome.issues[0].detail)
        self.assertEqual(self.docker.images[self.IMAGE], "old")
        self.assertIn(("compose", "media-automation", "start", "radarr"), self.docker.calls)


class SemverAutoTests(AutoCase):
    """An AdGuard minor under pin_minor: the exact tag pulled, retagged as v1.1, and the file moved from v1.0."""

    IMAGE = "adguard/adguardhome"
    CONF = "/opt/adguardhome/conf/AdGuardHome.yaml"

    def setUp(self):
        super().setUp()
        self.path = self.compose_file("adguard", f"services:\n  adguard:\n    image: {self.IMAGE}:v1.0\n")
        self.docker = FileDocker(
            {"adguard": Box(f"{self.IMAGE}:v1.0", "ag-old", "adguard", "adguard", (str(self.path),))},
            {f"{self.IMAGE}:v1.1.0": "ag-minor"},
            {"ag-old": "v1.0.0", "ag-minor": "v1.1.0"},
        )
        self.docker.archives[("adguard", self.CONF)] = b"schema 35"
        self.releases = [noted("v1.0.0", 60), noted("v1.1.0", 10, "- Faster lookups.")]
        self.app = replace(APPS["adguard"], auto="on")

    def update(self) -> Outcome:
        outcome = Outcome()
        with mock.patch.object(jobs, "github_releases", return_value=self.releases):
            update_semver(self.docker, CONFIG, set(self.docker.boxes), False, outcome, frozenset(), self.app, judge=self.judge(self.docker))
        return outcome

    def test_a_pinned_minor_goes_in_by_itself(self):
        outcome = self.update()
        self.assertEqual(self.path.read_text(), f"services:\n  adguard:\n    image: {self.IMAGE}:v1.1\n")
        self.assertIn(("pull", f"{self.IMAGE}:v1.1.0"), self.docker.calls)
        self.assertEqual(self.docker.images[f"{self.IMAGE}:v1.1"], "ag-minor")
        self.assertEqual(self.docker.boxes["adguard"].image_id, "ag-minor")
        self.assertEqual([(bump.before, bump.after, bump.reason) for bump in outcome.bumps], [("1.0.0", "1.1.0", "Only fixes.")])
        self.assertEqual(self.docker.ran[0][1][-3:], ["adguard: adguard to v1.1\n\nAdGuard Home 1.1.0 went in by itself. Only fixes.", "--", "adguard/docker-compose.yml"])
        self.assertEqual(outcome.issues, [])

    def test_a_plain_minor_still_waits_its_week_and_installs_itself(self):
        self.app = replace(self.app, pin_minor=False, wait_days=7)
        self.releases[1] = noted("v1.1.0", 3)
        outcome = self.update()
        self.assertEqual(self.asked, [])
        self.assertEqual(outcome.notices, [Notice("adguard", "1.0.0", "1.1.0", "https://github.com/AdguardTeam/AdGuardHome/releases/tag/v1.1.0", False)])


class ComposeFileTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)

    def write(self, name: str, text: str) -> str:
        path = self.dir / name
        path.write_bytes(text.encode())
        return str(path)

    def test_the_next_tag_keeps_the_shape(self):
        self.assertEqual(composefile.next_tag("2026.11", (2026, 12, 1)), "2026.12")
        self.assertEqual(composefile.next_tag("v3", (4, 0, 2)), "v4")
        self.assertEqual(composefile.next_tag("2.1-alpine", (2, 2, 0)), "2.2-alpine")
        self.assertIsNone(composefile.next_tag("latest", (1, 0)))
        self.assertIsNone(composefile.next_tag("1.2.3", (2, 0)))

    def test_retag(self):
        self.assertEqual(composefile.retag("ghcr.io/x/y:2026.11", "2026.12"), "ghcr.io/x/y:2026.12")
        self.assertEqual(composefile.retag("localhost:5000/app", "v2"), "localhost:5000/app:v2")
        with self.assertRaises(composefile.EditError):
            composefile.retag("x/y:1@sha256:00", "2")

    def test_only_the_image_line_changes(self):
        text = "name: x\r\nservices:\r\n  db:\r\n    image: postgres:16\r\n  app:\r\n    # pinned\r\n    image: 'x/app:v3'  # major\r\n    environment:\r\n      image: not-this\r\n"
        path = self.write("compose.yaml", text)
        edits = composefile.plan([path], [("app", "x/app:v3", "x/app:v4")])
        self.assertEqual(edits, [composefile.Edit(path, text, text.replace("'x/app:v3'", "'x/app:v4'"))])
        composefile.write(edits)
        self.assertEqual(Path(path).read_bytes(), text.replace("'x/app:v3'", "'x/app:v4'").encode())
        composefile.revert(edits)
        self.assertEqual(Path(path).read_bytes(), text.encode())

    def test_the_file_that_sets_the_image_is_the_one_edited(self):
        main = self.write("docker-compose.yml", "services:\n  app:\n    image: docker.io/library/nginx:1.27\n")
        override = self.write("docker-compose.override.yml", "services:\n  app:\n    ports:\n      - 80:80\n")
        edits = composefile.plan([main, override], [("app", "nginx:1.27", "nginx:1.28")])
        self.assertEqual([edit.path for edit in edits], [main])
        self.assertIn("docker.io/library/nginx:1.28", edits[0].after)

    def test_what_it_refuses(self):
        cases = [
            ("services:\n  app:\n    image: x:${TAG}\n", "not a plain image:tag"),
            ("services:\n  app:\n    image: x:1@sha256:00\n", "pinned by digest"),
            ("services:\n  app:\n    image: y:1\n", "says y:1 for app, but it runs x:1"),
            ("services:\n  app:\n    build: .\n", "no file of the project sets the image of app"),
            ("services:\n  app:\n    image: >-\n      x:1\n", "not a plain image:tag"),
        ]
        for text, message in cases:
            with self.subTest(message=message):
                path = self.write("docker-compose.yml", text)
                with self.assertRaises(composefile.EditError) as caught:
                    composefile.plan([path], [("app", "x:1", "x:2")])
                self.assertIn(message, str(caught.exception))
        first = self.write("a.yml", "services:\n  app:\n    image: x:1\n")
        second = self.write("b.yml", "services:\n  app:\n    image: x:1\n")
        with self.assertRaises(composefile.EditError):
            composefile.plan([first, second], [("app", "x:1", "x:2")])

    def test_a_service_block(self):
        text = "services:\n  db:\n    image: a\n\n  app:\n    image: b\n    ports:\n      - 1:1\n\nvolumes:\n  data:\n"
        self.assertEqual(composefile.block(text, "app"), "  app:\n    image: b\n    ports:\n      - 1:1\n")
        self.assertEqual(composefile.block(text, "data"), "")

    def test_only_images(self):
        before = {"services": {"app": {"image": "x:1", "environment": {"A": "1"}}}}
        after = {"services": {"app": {"image": "docker.io/x:2", "environment": {"A": "1"}}}}
        self.assertTrue(composefile.only_images(before, after, {"app": "x:2"}))
        self.assertFalse(composefile.only_images(before, after, {"app": "x:3"}))
        changed = {"services": {"app": {"image": "x:2", "environment": {"A": "2"}}}}
        self.assertFalse(composefile.only_images(before, changed, {"app": "x:2"}))


class NotesTests(unittest.TestCase):
    def test_short_notes_are_kept_whole(self):
        self.assertEqual(notes.relevant("  - Fixed a crash.\n"), "- Fixed a crash.")

    def test_long_notes_keep_their_breaking_sections_and_lines(self):
        filler = "\n".join(f"- Improved thing {index}." for index in range(200))
        text = f"# 4.0\n## Features\n{filler}\n## Breaking changes\n### Routers\n- The old syntax is gone.\n## Fixes\n- The API no longer accepts v1 tokens.\n- Fixed a typo."
        kept = notes.relevant(text)
        self.assertIn("## Breaking changes\n### Routers\n- The old syntax is gone.", kept)
        self.assertIn("## Fixes\n- The API no longer accepts v1 tokens.", kept)
        self.assertNotIn("Fixed a typo", kept)
        self.assertNotIn("Improved thing", kept)
        self.assertTrue(kept.endswith("[203 other lines left out: none mention a breaking change, deprecation, removal or migration]"))

    def test_between(self):
        releases = [release("v1.0.0", 30), release("v1.1.0", 20), Release((1, 2, 0), "v1.2.0-rc1", NOW - timedelta(days=15), prerelease=True), release("v1.2.0", 10)]
        self.assertEqual([item.tag for item in notes.between(releases, (1, 0, 0), (1, 2, 0))], ["v1.1.0", "v1.2.0-rc1", "v1.2.0"])
        self.assertEqual([item.tag for item in notes.between(releases, (1, 1, 0), (1, 2, 0))], ["v1.2.0-rc1", "v1.2.0"])
        self.assertEqual([item.tag for item in notes.between(releases[:2] + releases[3:], (1, 0, 0), (1, 1, 0))], ["v1.1.0"])
        with self.assertRaises(notes.NotesError):
            notes.between(releases[1:], (1, 0, 0), (1, 2, 0))
        with self.assertRaises(notes.NotesError):
            notes.between(releases, (1, 0, 0), (1, 3, 0))

    def test_links_named_as_notes_or_guides(self):
        body = "See the [release notes](https://a.test/blog#x), the [Migration Guide](https://a.test/m) and [#123](https://a.test/pr)."
        self.assertEqual(notes.links(body), ["https://a.test/blog#x", "https://a.test/m"])

    def test_bare_links_that_are_the_notes_or_named_as_them(self):
        self.assertEqual(notes.links("https://a.test/blog/release-20261/\n"), ["https://a.test/blog/release-20261/"])
        body = "> This is a **BREAKING** release, before updating, read https://a.test/d/1!\nMigration Guide: https://a.test/wiki/v2\nMore:\nhttps://a.test/beta.0\n- Fix (https://a.test/pr/2)"
        self.assertEqual(notes.links(body), ["https://a.test/d/1", "https://a.test/wiki/v2"])

    def test_changed_and_configuration_sections_are_kept(self):
        filler = "\n".join(f"- Improved thing {index}." for index in range(200))
        example = "  ```yaml\n  # BEFORE:\n  'querylog_interval': 1\n  ```\nTo roll back, set it to 1 again."
        text = f"## Added\n{filler}\n### Changed\n- Ports are checked for uniqueness.\n#### Configuration Changes\nThe schema version is now 12.\n{example}\n### Fixed\n- A typo."
        kept = notes.relevant(text)
        self.assertIn(f"### Changed\n- Ports are checked for uniqueness.\n#### Configuration Changes\nThe schema version is now 12.\n{example}", kept)
        self.assertNotIn("A typo", kept)

    def test_a_beta_tag_is_read_as_the_release_it_leads_to(self):
        import releases

        payload = [
            {"tag_name": "2.0.0-beta.1", "prerelease": True, "published_at": "2025-01-01T00:00:00Z", "body": "b"},
            {"tag_name": "2026.11.0b0", "prerelease": True, "published_at": "2026-10-29T00:00:00Z"},
            {"tag_name": "2.0.0-final", "prerelease": False, "published_at": "2025-02-01T00:00:00Z"},
        ]
        with mock.patch.object(releases, "_get_json", return_value=payload):
            found = releases.github_releases("a/b")
        self.assertEqual([(item.tag, item.version, item.prerelease) for item in found], [("2.0.0-beta.1", (2, 0, 0), True), ("2026.11.0b0", (2026, 11, 0), True)])
        self.assertIsNone(releases.latest_stable(found))

    def test_a_page_as_text_and_its_linked_section(self):
        html = """<html><head><style>p{}</style></head><body><nav>Menu</nav>
        <h2 id="new">New</h2><p>Lots of   <b>new</b> things.</p>
        <h2 id="bic">Backward-incompatible changes</h2>
        <details><summary><div class="arrow"></div><span>MQTT</span></summary><p>The <a href="/integrations/mqtt/">MQTT</a> option moved.</p></details>
        <h2 id="after">Farewell</h2><ul><li>Old thing</li></ul></body></html>"""
        whole = notes.page_text(html)
        self.assertIn("Lots of new things.", whole)
        self.assertNotIn("Menu", whole)
        section = notes.page_text(html, "bic")
        self.assertEqual(section.splitlines()[0], "## Backward-incompatible changes")
        self.assertIn("**MQTT**", section)
        self.assertIn("The MQTT (/integrations/mqtt/) option moved.", section)
        self.assertNotIn("Old thing", section)

    def test_named_terms_and_compose_terms(self):
        text = "Removed `--providers.docker.swarmMode`; see /integrations/zha/. PUID is unchanged."
        self.assertEqual(notes.named(text, ["zha", "mqtt"], "/integrations/{}/"), ["zha"])
        self.assertEqual(notes.named("- Fix ([mqtt docs])\n\n[mqtt docs]: https://www.home-assistant.io/integrations/mqtt/", ["mqtt"], "/integrations/{}/"), [])
        block = "  app:\n    command:\n      - --providers.docker.swarmMode=true\n    environment:\n      - PUID=1000\n      TZ: x\n"
        terms = notes.compose_terms(block)
        self.assertTrue({"--providers.docker.swarmMode", "providers.docker.swarmMode", "PUID", "TZ"} <= terms)
        self.assertEqual(notes.named(text, terms), ["--providers.docker.swarmMode", "PUID"])

    def test_undone(self):
        self.assertEqual(notes.undone("Fine. The database is migrated and can't be rolled back. Done."), ["The database is migrated and can't be rolled back"])
        self.assertEqual(notes.undone("Rolled back the change."), [])


class VerdictTests(unittest.TestCase):
    ENV = {"CLOUDFLARE_ACCOUNT_ID": "acct", "CLOUDFLARE_AI_TOKEN": "token"}
    QUESTION = verdict.Question("Traefik", "3.6.2", "4.0.1", "traefik traefik:v3 → traefik:v4", "  traefik:\n    image: traefik:v3", notes="- Fixes.")

    def test_parse(self):
        found = verdict.parse('Here:\n```json\n{"decision": "auto", "reasons": ["a", "b", "c", "d"], "actions_needed": []}\n```')
        self.assertEqual(found, Verdict("auto", ("a", "b", "c")))
        self.assertTrue(found.auto)
        self.assertFalse(Verdict("auto", (), ("Edit a file.",)).auto)
        self.assertEqual(verdict.parse('{"decision": "manual", "reasons": ["a"]}'), Verdict("manual", ("a",)))
        for text in ("no json", '{"decision": "maybe"}', '{"decision": "auto", "reasons": "a"}', '{"decision": "manual", "actions_needed": [1]}',
                     '{"decision": "auto", "reasons": ["a"], "actions": ["Run the migration."]}'):
            with self.subTest(text=text), self.assertRaises(VerdictError):
                verdict.parse(text)

    def test_the_request(self):
        sent = []

        class Reply(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        def urlopen(request, timeout):
            sent.append((request, timeout))
            answer = {"decision": "manual", "reasons": ["A flag was renamed."], "actions_needed": ["Rename it."]}
            return Reply(json.dumps({"choices": [{"message": {"content": json.dumps(answer)}}]}).encode())

        with mock.patch.object(verdict.urllib.request, "urlopen", urlopen):
            found = verdict.ask(self.QUESTION, self.ENV)
        self.assertEqual(found, Verdict("manual", ("A flag was renamed.",), ("Rename it.",)))
        self.assertEqual(json.loads(found.said)["actions_needed"], ["Rename it."])
        request, timeout = sent[0]
        self.assertEqual(request.full_url, "https://api.cloudflare.com/client/v4/accounts/acct/ai/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer token")
        body = json.loads(request.data)
        self.assertEqual((body["model"], body["max_tokens"], body["temperature"]), ("@cf/openai/gpt-oss-120b", 4096, 0))
        self.assertEqual(body["messages"][0], {"role": "system", "content": verdict.SYSTEM})
        self.assertIn("<notes>\n- Fixes.\n</notes>", body["messages"][1]["content"])
        self.assertEqual(timeout, 180)

    def test_errors(self):
        def refused(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 429, "Too Many Requests", {}, io.BytesIO(b"daily limit"))

        def failing(code: int, body: bytes):
            def urlopen(request, timeout):
                raise urllib.error.HTTPError(request.full_url, code, "", {}, io.BytesIO(body))
            return urlopen

        with mock.patch.object(verdict.urllib.request, "urlopen", refused), self.assertRaises(verdict.Unavailable) as caught:
            verdict.ask(self.QUESTION, self.ENV)
        self.assertEqual(str(caught.exception), "it has no tokens left today (HTTP 429)")
        for code, body, said in (
            (400, b'{"errors": [{"code": 4006, "message": "used up your daily free allocation"}]}', "it has no tokens left today (HTTP 400)"),
            (401, b"bad token", "it refused the token (HTTP 401)"),
        ):
            with mock.patch.object(verdict.urllib.request, "urlopen", failing(code, body)), self.assertRaises(verdict.Unavailable) as caught:
                verdict.ask(self.QUESTION, self.ENV)
            self.assertEqual(str(caught.exception), said)
        with mock.patch.object(verdict.urllib.request, "urlopen", failing(500, b"busy")), self.assertRaises(VerdictError) as caught:
            verdict.ask(self.QUESTION, self.ENV)
        self.assertNotIsInstance(caught.exception, verdict.Unavailable)
        self.assertIn("HTTP 500: busy", str(caught.exception))
        with self.assertRaises(VerdictError):
            verdict._content({"choices": [{"message": {"content": ""}}]})
        self.assertEqual(verdict._content({"result": {"response": "{}"}}), "{}")

    def test_configured(self):
        self.assertTrue(verdict.configured(self.ENV))
        self.assertFalse(verdict.configured({"CLOUDFLARE_ACCOUNT_ID": "acct"}))


class ShownTextTests(unittest.TestCase):
    def test_an_unclosed_item_in_a_skipped_menu_keeps_the_rest_of_the_page(self):
        page = '<nav><ul><li>Home<li>Blog</ul></nav><h2>Breaking changes</h2><p>X was removed.</p>'
        self.assertEqual(notes.page_text(page), "## Breaking changes\n\nX was removed.")
        self.assertEqual(notes.page_text('<nav><nav>a</nav>b</nav><p>c<span class="tooltip">d<span>e</span></span> f</p>'), "c f")

    def test_cant_be_undone_is_found_quickly_with_its_sentence(self):
        started = time.monotonic()
        self.assertEqual(notes.undone("word " * 20000 + "so it cannot be undone"), ["word " * 17 + "word so it cannot be undone"])
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(notes.undone("Fixes. Downgrading is not supported, sorry.\nIt can't be undone, it is irreversible."),
                         ["Downgrading is not supported, sorry", "It can't be undone, it is irreversible"])

    def test_trailing_compose_comments_are_dropped(self):
        self.assertEqual(auto._uncommented('    image: "traefik:v3"   # after a major, run the tool'), '    image: "traefik:v3"')
        for line in ("      - 'a # b'", "      URL: http://x/#frag", '    command: ["--x=#1"]'):
            self.assertEqual(auto._uncommented(line), line)

    def test_secret_values_in_files_shown_are_hidden(self):
        text = "- trigger: webhook\n  webhook_id: abc123\n  api_key: !secret k\n  entity_id: cover.curtains\n"
        self.assertEqual(auto.hide_secrets(text), "- trigger: webhook\n  webhook_id: (hidden)\n  api_key: (hidden)\n  entity_id: cover.curtains\n")
        text = ("rest_command:\n  open:\n    headers:\n      authorization: \"Bearer abc\"\n    private_key: |\n      line one\n\n      line two\n"
                "    url: http://x\n- password: >-\n    folded\n  name: kept\n")
        self.assertEqual(auto.hide_secrets(text), "rest_command:\n  open:\n    headers:\n      authorization: (hidden)\n    private_key: (hidden)\n"
                                                  "    url: http://x\n- password: (hidden)\n  name: kept\n")

    def test_the_inventory_takes_each_entry_s_own_name_only(self):
        entries = {"data": {"entries": [{"domain": "duckdns", "data": {"domain": "home.duckdns.org"}}, {"domain": "mqtt", "discovery_keys": {"x": [{"domain": "zeroconf"}]}}]}}
        self.assertEqual(sorted(set(auto._values(entries, "domain"))), ["duckdns", "mqtt"])

    def test_the_journal_keeps_a_year(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder)
        for name in ("2025-10", "2025-11", "2026-10"):
            (folder / f"{name}.jsonl").write_text("{}\n")
        auto.Judge(FakeDocker({}), {}, {}, today=date(2026, 11, 27), journal=folder)._journal({"app": "x"})
        self.assertEqual(sorted(path.stem for path in folder.glob("*.jsonl")), ["2025-11", "2026-10", "2026-11"])

    def test_an_answer_with_no_message_is_no_answer(self):
        with self.assertRaises(VerdictError):
            verdict._content({"choices": [{"message": "text"}]})


class JudgeRuleTests(unittest.TestCase):
    def test_code_can_only_say_no(self):
        said = Verdict("auto", ("Only fixes.",))
        self.assertEqual(auto.combine(said, []), said)
        self.assertEqual(auto.combine(said, ["Names mqtt."]), Verdict("manual", ("Names mqtt.", "Only fixes.")))
        manual = Verdict("manual", ("A step.",), ("Do it.",))
        self.assertEqual(auto.combine(manual, []), manual)



class AutoConfigTests(unittest.TestCase):
    BASE = ConfigTests.BASE
    load = ConfigTests.load
    rejects = ConfigTests.rejects
    APP = '\n[apps.x]\ntitle = "X"\ncontainers = ["x"]\nreleases = "github:a/b"\n'

    def test_mistakes_stop_the_run(self):
        app = self.BASE + self.APP
        cases = [
            (app + 'policy = "floating"\nauto = "yes"\n', "apps.x.auto must be one of off, shadow, on"),
            (self.BASE + '\n[apps.x]\ntitle = "X"\ncontainers = ["x"]\nreleases = "hub:a/b"\npolicy = "floating"\nauto = "on"\n', "needs github: releases"),
            (app + 'policy = "report"\nauto = "shadow"\n', "a report app is never changed"),
            (app + 'policy = "semver"\nauto = "on"\n', "needs rollback = true on a semver app"),
            (app + 'policy = "floating"\nauto = "on"\nimage = "x:1"\n', "image pins it"),
            (app + 'policy = "floating"\ninventory_file = "/a.json"\ninventory_key = "domain"\n', "apps.x.inventory_file needs auto"),
            (app + 'policy = "floating"\nlinked_notes = true\n', "apps.x.linked_notes needs auto"),
            (app + 'policy = "floating"\nauto = "on"\ninventory_file = "/a.json"\n', "inventory_file and inventory_key go together"),
            (app + 'policy = "floating"\nauto = "on"\ninventory_file = "/a.json"\ninventory_key = "k"\ninventory_match = "/x/"\n', "must hold {} once"),
            (app + 'policy = "floating"\nauto = "on"\nlinked_notes = "yes"\n', "linked_notes must be true or false"),
            (app + 'policy = "floating"\nauto = "on"\nrollback_files = ["/"]\n', "below /"),
            (app + 'policy = "floating"\nauto = "on"\nrollback_files = ["/a/../b"]\n', "below /"),
            (app + 'policy = "floating"\nauto = "on"\ncontext_files = ["a.yaml"]\n', "context_files must be a list of absolute paths"),
            (app + 'policy = "floating"\nrollback_files = ["/a"]\n', "needs rollback = true, or auto on a floating app"),
            (self.BASE.replace('containers = ["dockge"]', 'containers = ["dockge"]\nreleases = "github:a/b"\nauto = "on"\nrollback_files = ["/a"]'), "can't be stopped"),
            (app + 'policy = "floating"\nwait_days = 7\n', "wait_days is for semver apps and apps with auto"),
            (app + 'policy = "semver"\nwait_days = -1\n', "wait_days must be a whole number of days"),
            (app + 'policy = "semver"\nwait_days = true\n', "wait_days must be a whole number of days"),
            (app + 'policy = "semver"\nwait_days = "7"\n', "wait_days must be a whole number of days"),
            (app + 'policy = "semver"\ncritical = 1\n', "critical must be true or false"),
            (app + 'policy = "floating"\nauto = "on"\nauto_major = true\n', "apps.x: unknown field auto_major"),
            (app + 'policy = "floating"\nbreaking_marker = "(breaking-change)"\n', "apps.x.breaking_marker needs auto"),
            (app + 'policy = "semver"\nrollback = true\nauto = "on"\nbreaking_marker = "(breaking-change)"\n', "breaking_marker is for floating apps"),
            (app + 'policy = "floating"\nauto = "on"\nbreaking_marker = "(b)"\nbreaking_names = "[{} docs]"\n', "needs breaking_marker and inventory_file"),
            (app + 'policy = "floating"\nauto = "on"\nbreaking_marker = "(b)"\ninventory_file = "/a"\ninventory_key = "k"\nbreaking_names = "docs"\n', "breaking_names must hold {} once"),
        ]
        for text, message in cases:
            with self.subTest(message=message):
                self.rejects(text, message)

    def test_only_immich_waits(self):
        self.assertEqual({key: app.wait_days for key, app in APPS.items() if app.wait_days}, {"immich": 7})

    def test_photos_dns_and_ingress_are_critical(self):
        self.assertEqual({key for key, app in APPS.items() if app.critical}, {"immich", "adguard", "traefik"})

    def test_explain_says_what_each_release_does(self):
        hand = "emailed, installed by hand"
        shadow = "emailed with Workers AI's verdict (shadow), installed by hand"
        asked = "Workers AI is asked: by itself if nothing needs doing by hand, else emailed"
        cases = [
            ("homeassistant", "2026.11.3", [("patch", asked), ("minor", asked), ("major", asked)]),
            ("zigbee2mqtt", "2.14", [("patch", "by itself"), ("minor", shadow), ("major", shadow)]),
            ("radarr", "latest", [("patch", "by itself"), ("minor", "by itself"), ("major", asked)]),
            ("traefik", "v3", [("patch", "by itself"), ("minor", "by itself"), ("major", hand)]),
            ("mosquitto", "2.1-alpine", [("patch", "by itself"), ("minor", hand), ("major", hand)]),
            ("adguard", "v0.107.70", [("patch", "by itself"), ("minor", shadow), ("major", hand)]),
            ("immich", "v3.1.0", [("patch", "by itself"), ("minor", "by itself after 7 days"), ("major", hand)]),
            ("plex", "", [("newer release", hand)]),
        ]
        for key, tag, expected in cases:
            with self.subTest(key=key):
                self.assertEqual(jobs.explain(APPS[key], tag), expected)

    def test_explain_with_auto_on_and_a_wait(self):
        app = replace(APPS["homeassistant"], auto="on", wait_days=3)
        self.assertEqual(jobs.explain(app, "2026.11")[1][1], "Workers AI is asked, 3 days after it is out: by itself if nothing needs doing by hand, else emailed")
        self.assertEqual(jobs.explain(replace(app, releases=""), "2026.11")[1][1], "not checked: no releases to compare with")

    def test_check_fails_when_a_critical_app_would_take_a_major_by_itself(self):
        loaded = self.load(self.BASE.replace('containers = ["traefik"]', 'containers = ["traefik"]\ncritical = true'))
        boxes = {"traefik": Box("traefik:latest", "sha256:t", "traefik", "traefik"), "dockge": Box("louislam/dockge:1", "sha256:d", "dockge", "dockge")}
        lines = []
        with mock.patch.object(update, "log", lines.append):
            self.assertFalse(update.summary(FakeDocker(boxes), loaded))
        self.assertIn("  Traefik (:latest, critical)", lines)
        self.assertTrue(any("is critical, but :latest takes a new major by itself" in line for line in lines))
        boxes["traefik"] = Box("traefik:v3", "sha256:t", "traefik", "traefik")
        with mock.patch.object(update, "log", lines.append):
            self.assertTrue(update.summary(FakeDocker(boxes), loaded))

    def test_an_auto_app_loads(self):
        loaded = self.load(self.BASE + self.APP + 'policy = "floating"\nauto = "shadow"\ninventory_file = "/i.json"\ninventory_key = "domain"\ninventory_match = "/i/{}/"\nrollback_files = ["/data/app.db"]\n')
        app = loaded.apps["x"]
        self.assertEqual((app.auto, app.critical, app.inventory_match, app.rollback_files), ("shadow", False, "/i/{}/", ("/data/app.db",)))
        self.assertEqual(self.load(self.BASE).apps["traefik"].auto, "")

    def test_a_major_goes_in_by_itself_only_where_config_toml_says_so(self):
        self.assertEqual([key for key, app in APPS.items() if app.auto and app.critical], ["adguard"])

    def test_the_shipped_config_says_which_apps_workers_ai_may_install(self):
        modes = {key: app.auto for key, app in APPS.items() if app.auto}
        self.assertEqual({key for key, mode in modes.items() if mode == "on"}, {"uptime-kuma", "radarr", "sonarr", "prowlarr", "maintainerr", "seerr", "homeassistant"})
        self.assertEqual({key for key, mode in modes.items() if mode == "shadow"}, {"adguard", "zigbee2mqtt"})
        self.assertEqual([key for key, app in APPS.items() if app.critical and app.auto == "on"], [])
        for key in ("immich", "traefik", "opencloud-opencloud-1", "mosquitto", "qbittorrent", "beszel", "dockge"):
            self.assertNotIn(key, modes)
        self.assertNotIn("/app/data/configuration.yaml", APPS["zigbee2mqtt"].context_files)


class AutoStateTests(StateTests):
    def setUp(self):
        super().setUp()
        for name, value in (("VERDICTS", self.dir / "state" / "verdicts.tsv"), ("ASKING", self.dir / "state" / "asking.tsv")):
            patcher = mock.patch.object(update, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_verdicts_are_kept_and_read_back(self):
        judge = auto.Judge(FakeDocker({}), {}, {})
        judge.fresh = [
            ("traefik", "4.0.1", Verdict("manual", ("A | flag.", "Two."), ("Rename it.",), held=HELD_AI), "0123456789abcdef"),
            ("seerr", "4.0.0", Verdict("auto", ("Fixes.",), held=HELD_AI), "fedcba9876543210"),
        ]
        with mock.patch.dict(os.environ, {"UPDATE_TODAY": "2026-11-20"}):
            update._keep_verdicts(judge)
        self.assertEqual(update._load_verdicts(), ({
            ("traefik", "4.0.1"): Verdict("manual", ("A / flag.", "Two."), ("Rename it.",)),
            ("seerr", "4.0.0"): Verdict("auto", ("Fixes.",)),
        }, {("traefik", "4.0.1"): "0123456789abcdef", ("seerr", "4.0.0"): "fedcba9876543210"}))
        self.assertEqual(update._load_verdicts()[0][("traefik", "4.0.1")].held, HELD_AI)
        judge.fresh = [("traefik", "4.0.1", Verdict("manual", ("Rolled back.",)), "")]
        with mock.patch.dict(os.environ, {"UPDATE_TODAY": "2027-12-01"}):
            update._keep_verdicts(judge)
        self.assertEqual(update._load_verdicts(), ({("traefik", "4.0.1"): Verdict("manual", ("Rolled back.",))}, {("traefik", "4.0.1"): ""}))

    def test_verdicts_that_cant_be_kept_or_read_are_an_issue_not_a_crash(self):
        judge = auto.Judge(FakeDocker({}), {}, {})
        judge.fresh = [("traefik", "4.0.1", Verdict("manual", ("A flag.",)), "abc")]
        with mock.patch.object(update, "_write", side_effect=PermissionError("verdicts.tsv")):
            issues = update._keep_verdicts(judge)
        self.assertEqual([issue.name for issue in issues], ["updater"])
        self.assertIn("asks again", issues[0].detail)
        config = replace(CONFIG, apps={"traefik": replace(APPS["traefik"], auto="on")})
        with mock.patch.object(update, "_read_env", return_value=VerdictTests.ENV), mock.patch.object(update, "_load_verdicts", side_effect=PermissionError("verdicts.tsv")):
            found = update._judge(FakeDocker({}), config, asking=True)
        self.assertIn("earlier answers could not be read", found.unavailable)

    def test_a_verdict_kept_before_it_said_who_gave_it_still_reads(self):
        update.VERDICTS.parent.mkdir(parents=True, exist_ok=True)
        update.VERDICTS.write_text("2026-11-20\ttraefik\t4.0.1\tmanual\tA flag.\t\tabc\n")
        found = update._load_verdicts()[0][("traefik", "4.0.1")]
        self.assertEqual((found, found.held), (Verdict("manual", ("A flag.",)), ""))

    def test_a_release_with_no_answer_is_remembered_until_it_has_one(self):
        judge = auto.Judge(FakeDocker({}), {}, {}, failures={("traefik", "4.0.1"): date(2026, 11, 20)})
        with mock.patch.dict(os.environ, {"UPDATE_TODAY": "2026-11-21"}):
            update._keep_verdicts(judge)
        self.assertEqual(update._load_asking(), {("traefik", "4.0.1"): date(2026, 11, 20)})
        judge.failures.clear()
        update._keep_verdicts(judge)
        self.assertFalse(update.ASKING.exists())

    def test_a_release_waiting_for_an_answer_is_neither_pushed_nor_marked_as_mailed(self):
        pending = Notice("traefik", "3.6.2", "4.0.1", TRAEFIK_URL, False, "pending")
        update._remember_notices([pending])
        self.assertEqual(update._pending([pending]), [pending])
        (self.dir / ".env").write_text("NTFY_TOPIC=t\n")
        update._push_releases([pending], False)
        self.assertEqual(self.pushes, [])

    def test_a_judge_needs_the_cloudflare_settings(self):
        docker = FakeDocker({})
        self.assertEqual(update._judge(docker, CONFIG, asking=True).unavailable,
                         "Workers AI was not asked: CLOUDFLARE_ACCOUNT_ID or CLOUDFLARE_AI_TOKEN is missing in .env.")
        (self.dir / ".env").write_text("CLOUDFLARE_ACCOUNT_ID=acct\nCLOUDFLARE_AI_TOKEN=token\n")
        judge = update._judge(docker, CONFIG, asking=False)
        self.assertEqual((judge.asking, judge.env["CLOUDFLARE_ACCOUNT_ID"], judge.unavailable), (False, "acct", ""))
        self.assertEqual(judge.journal, update.JOURNAL)
        quiet = replace(CONFIG, apps={key: replace(app, auto="") for key, app in APPS.items()})
        self.assertIsNone(update._judge(docker, quiet, asking=True))

    def test_a_release_mailed_while_it_waits_is_pushed_once_judged(self):
        waiting = Notice("traefik", "3.6.2", "4.0.1", TRAEFIK_URL, False, "wait")
        update._remember_notices([waiting])
        self.assertEqual(update._pending([waiting]), [waiting])
        judged = replace(waiting, by_hand=True, verdict="manual", reasons=("A flag was renamed.",))
        (self.dir / ".env").write_text("NTFY_TOPIC=t\n")
        update._push_releases([judged], False)
        self.assertEqual([item for item, _topic, _server in self.pushes], [judged])

    def test_the_reason_of_a_bump_is_kept_for_the_sunday_mail(self):
        kept = merge_bumps([], [Bump("traefik", "3.6.2", "4.0.1", "https://x.test", "Only\tfixes.")], SUNDAY)
        update._save_bumps(kept + [Kept("homer", "1", "2", "", SUNDAY)])
        loaded = update._load_bumps()
        self.assertEqual([item.as_bump() for item in loaded], [Bump("traefik", "3.6.2", "4.0.1", "https://x.test", "Only fixes."), Bump("homer", "1", "2", "")])
        self.assertEqual(update.BUMPS.read_text().splitlines()[1], f"homer\t1\t2\t\t{SUNDAY.isoformat()}")


class AutoMessageTests(unittest.TestCase):
    MANUAL = Notice("traefik", "3.6.2", "4.0.1", TRAEFIK_URL, True, "manual", ("The dashboard flag was renamed.",), ("Rename --api.dashboard.", "Restart."))
    SHADOW = Notice("seerr", "3.4.0", "4.0.0", "https://x.test", True, "shadow", ("Only fixes.",))
    MOVED = Bump("homeassistant", "2026.11.3", "2026.12.1", "https://x.test", "Nothing set up changed.")

    def test_pushes(self):
        self.assertEqual(push.message(self.MANUAL, "t")["message"], "You run 3.6.2. A new major, install it by hand. The dashboard flag was renamed. To do: Rename --api.dashboard; Restart.")
        self.assertEqual(push.message(self.SHADOW, "t")["message"], "You run 3.4.0. Shadow mode: it would have gone in by itself. Install it by hand. Only fixes.")
        found = push.message(replace(self.MANUAL, held=HELD_AI), "t")
        self.assertEqual((found["title"], found["message"]), ("Traefik 4.0.1 is out",
            "You run 3.6.2. A new major, install it by hand. Breaking changes detected by AI. The dashboard flag was renamed. To do: Rename --api.dashboard; Restart."))
        patch = Notice("homeassistant", "2026.11.3", "2026.11.4", "https://x.test", True, "manual", ("“Change ZHA groups (breaking-change)”", "A second reason."), held=HELD_BREAKING)
        self.assertEqual(push.message(patch, "t")["message"],
                         "You run 2026.11.3. Install it by hand. Breaking changes detected in the notes. “Change ZHA groups (breaking-change)”")
        self.assertEqual(push.message(self.MOVED, "t")["message"], "Went in by itself, checked by AI, and is running.")

    def test_the_mail_shows_why(self):
        letter = pinned_letter([self.MANUAL, self.SHADOW], [self.MOVED])
        self.assertIn("  Safe, checked by AI.", letter.text)
        self.assertIn('>Safe · AI</span>', letter.html)
        self.assertNotIn("Nothing set up changed", letter.html)
        self.assertIn("  By hand. The dashboard flag was renamed. To do: Rename --api.dashboard; Restart.", letter.text)
        self.assertIn("  Would have gone in by itself (shadow mode). Only fixes.", letter.text)
        found = pinned_letter([replace(self.MANUAL, held=HELD_AI, reasons=("The dashboard flag was renamed.", "A second reason."))], [])
        self.assertIn("  By hand. Breaking changes detected by AI. The dashboard flag was renamed. A second reason. To do: Rename --api.dashboard; Restart.", found.text)
        self.assertIn(">To do</div>", letter.html)
        self.assertIn('&#8226;&nbsp;&nbsp;</span>Rename <span style="white-space:nowrap">--api.dashboard</span></div>', letter.html)
        self.assertEqual(letter.html.count('class="m-phone m-muted" style="display:none;'), 3)
        self.assertIn(">Shadow</span>", letter.html)
        self.assertNotIn("By hand.", letter.html)
        tagged = pinned_letter([replace(self.MANUAL, held=HELD_AI), replace(self.SHADOW, verdict="manual", held=HELD_BREAKING),
                                replace(self.SHADOW, key="homer", verdict="manual", unanswered=True)], [])
        self.assertEqual(re.findall(r'class="(m-warn|m-info|m-quiet)"[^>]*>([^<]+)</span>', tagged.html),
                         [("m-warn", "Breaking · AI"), ("m-warn", "Breaking"), ("m-quiet", "Not checked")])


if __name__ == "__main__":
    unittest.main()
