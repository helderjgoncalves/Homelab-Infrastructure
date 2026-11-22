"""Rules, state and mail, without Docker."""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
import urllib.parse
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import config
import jobs
import mail
import update
from config import ConfigError
from jobs import HOLD_NOTE, Outcome, apply_update, run, update_floating, update_semver
from mail import Bump, Issue, Notice, failure_letter, pinned_letter
from releases import Firmware, Release, choose, newer, parse_version, qts_firmware
from runtime import Container, DockerError, Stack, api_prefix, stack_of, unused_image_prune_path
from update import Kept, digest_due, merge_bumps

NOW = datetime(2026, 11, 12, tzinfo=timezone.utc)
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
        tag, waiting = choose("v3.4.1", [release("v3.5.0", 6)], NOW)
        self.assertIsNone(tag)
        self.assertEqual(waiting.tag, "v3.5.0")

    def test_new_minor_is_taken_after_a_week(self):
        tag, waiting = choose("3.4.1", [release("v3.5.0", 7)], NOW)
        self.assertEqual(tag, "v3.5.0")
        self.assertIsNone(waiting)

    def test_patch_of_a_new_minor_does_not_wait(self):
        tag, _waiting = choose("v3.4.1", [release("v3.5.0", 1), release("v3.5.1", 0)], NOW)
        self.assertEqual(tag, "v3.5.1")

    def test_a_newer_major_is_held_and_prereleases_are_ignored(self):
        tag, waiting = choose("v3.7.18", [release("v4.0.0", 30), release("v4.1.0", 2), release("v3.8.0", 1, prerelease=True)], NOW)
        self.assertIsNone(tag)
        self.assertEqual(waiting.tag, "v4.1.0")
        self.assertEqual(choose("v3.7.18", [release("v2.9.0", 1)], NOW), (None, None))

    def test_a_waiting_minor_is_named_before_a_new_major(self):
        tag, waiting = choose("v3.4.1", [release("v3.4.2", 0), release("v3.5.0", 1), release("v4.0.0", 0)], NOW)
        self.assertEqual((tag, waiting.tag), ("v3.4.2", "v3.5.0"))
        tag, waiting = choose("v3.4.1", [release("v3.5.0", 8), release("v4.0.0", 0)], NOW)
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


class StateTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        state = self.dir / "state"
        paths = {
            "STATE": state,
            "NOTIFIED": state / "notified.tsv",
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


class MainTests(StateTests):
    def setUp(self):
        super().setUp()
        self.docker = FakeDocker({})
        self.outcome = Outcome()
        self.run = mock.Mock(side_effect=lambda *_args: self.outcome)
        for name, value in (
            ("Docker", lambda: self.docker),
            ("run", self.run),
            ("collect_notices", lambda *_args: None),
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
        def stopped(_docker, _config, _dry, _rejected, outcome):
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
    }
    IMMICH = ("immich_server", "immich_machine_learning")
    APP_VERSION = {"uptime-kuma": "/app/package.json", "dockge": "/app/package.json"}
    VERSION_COMMAND = {"cloudflare-tunnel": ("cloudflared", "--version")}
    TITLES = {
        "adguard": "AdGuard Home", "homeassistant": "Home Assistant", "zigbee2mqtt": "Zigbee2MQTT",
        "mosquitto": "Mosquitto", "opencloud-opencloud-1": "OpenCloud", "traefik": "Traefik",
        "cloudflare-tunnel": "Cloudflared", "beszel": "Beszel", "uptime-kuma": "Uptime Kuma",
        "dockge": "Dockge", "immich": "Immich", "plex": "Plex", "qts": "QTS", "homer": "Homer",
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
        with mock.patch.object(jobs, "update_floating", lambda *args: order.append(args[-1].key)), \
                mock.patch.object(jobs, "update_semver", lambda *args: order.append(args[-1].key)), \
                mock.patch.object(jobs, "collect_pinned", lambda *args: order.append("pinned")), \
                mock.patch.object(jobs, "collect_outside", lambda *args: order.append("outside")):
            run(FakeDocker({}), CONFIG, False)
        self.assertEqual(order, [
            "immich", "cloudflare-tunnel", "traefik", "uptime-kuma", "beszel", "adguard",
            "homeassistant", "zigbee2mqtt", "mosquitto", "homer", "pinned", "outside", "dockge",
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


if __name__ == "__main__":
    unittest.main()
