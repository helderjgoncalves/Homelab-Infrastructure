"""Rules, state and mail, without Docker."""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
import urllib.parse
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import jobs
import update
from jobs import HOLD_NOTE, Outcome, apply_update, run, update_beszel, update_immich, update_one
from mail import Bump, Issue, Notice, failure_letter, pinned_letter
from releases import Release, choose, newer, parse_version
from runtime import Container, DockerError, api_prefix, unused_image_prune_path
from update import Kept, digest_due, merge_bumps

NOW = datetime(2026, 11, 12, tzinfo=timezone.utc)
STACK = Path("/stacks/x/docker-compose.yml")
SUNDAY = date(2026, 11, 15)


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
        self.pruned = 0
        self.prune_error: str | None = None
        self.files: dict[tuple[str, str], bytes] = {}
        self.outputs: dict[tuple[str, str], str] = {}

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
        return Container(name, box.image, box.image_id, version, "running", "healthy")

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

    def compose(self, project: str, file: Path, *args: str) -> None:
        self.calls.append(("compose", project, *args))
        services = [arg for arg in args[1:] if not arg.startswith("-") and arg != "never"]
        boxes = [box for box in self.boxes.values() if box.project == project and box.service in services]
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

    def recreate_dockge(self, image: str, file: Path) -> None:
        self.compose("dockge", file, "up", "-d", "dockge")

    def wait_ready(self, name: str) -> bool:
        return self.boxes[name].image_id not in self.unhealthy

    def prune_unused(self) -> tuple[int, int]:
        if self.prune_error:
            raise DockerError(self.prune_error)
        self.pruned += 1
        return 3, 1048576


def traefik(remote: str = "new", image: str = "traefik:v3") -> FakeDocker:
    return FakeDocker({"traefik": Box(image, "old", "traefik", "traefik")}, {image: remote})


def bump_traefik(docker: FakeDocker, outcome: Outcome, rejected=frozenset()) -> None:
    update_one(docker, set(docker.boxes), False, outcome, rejected, "traefik", STACK, "traefik", "traefik", "traefik:v3")


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

    def test_other_majors_and_prereleases_are_ignored(self):
        tag, waiting = choose("v3.7.18", [release("v4.0.0", 30), release("v3.8.0", 1, prerelease=True)], NOW)
        self.assertIsNone(tag)
        self.assertIsNone(waiting)


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

    def test_traefik_stays_on_v3(self):
        docker = traefik(image="traefik:v4")
        outcome = Outcome()
        bump_traefik(docker, outcome)
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
            if name == "dockge":
                jobs.update_dockge(docker, {name}, False, outcome, set())
            else:
                update_one(docker, {name}, False, outcome, set(), name, STACK, name, name)
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
        update_one(docker, {"cloudflare-tunnel"}, False, outcome, set(), "cloudflared", STACK, "cloudflare-tunnel", "cloudflare-tunnel")
        self.assertEqual(outcome.bumps, [Bump("cloudflare-tunnel", "2026.10.7", "2026.11.0", "https://github.com/cloudflare/cloudflared/releases/tag/2026.11.0")])

    def test_an_unreadable_package_falls_back_to_the_tag(self):
        docker = FakeDocker({"uptime-kuma": Box("louislam/uptime-kuma:2", "old", "uptime-kuma", "uptime-kuma")}, {"louislam/uptime-kuma:2": "new"})
        docker.files = {("uptime-kuma", "new"): b"not json"}
        outcome = Outcome()
        update_one(docker, {"uptime-kuma"}, False, outcome, set(), "uptime-kuma", STACK, "uptime-kuma", "uptime-kuma")
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
        update_beszel(docker, {"beszel"}, False, outcome, set())
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
        update_immich(self.docker, running or set(self.docker.boxes), False, outcome, rejected)
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
        self.assertIn("IMMICH_VERSION", outcome.issues[0].detail)
        self.assertEqual(outcome.bumps, [])

    def test_failed_pull_changes_nothing(self):
        del self.docker.remote[f"{self.LEARNING}:v3.4.2"]
        outcome = self.update()
        self.assertEqual(self.docker.images[f"{self.SERVER}:v3"], "srv-old")
        self.assertFalse([call for call in self.docker.calls if call[0] == "compose"])
        self.assertIn("Neither container was changed", outcome.issues[0].detail)

    def test_server_alone_is_left_alone(self):
        self.update(running={"immich_server"})
        self.assertEqual(self.docker.calls, [])


class RunTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"PLEX_VERSION": ""})
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
        outcome = run(docker, False)
        self.assertEqual([bump.key for bump in outcome.bumps], ["traefik"])
        self.assertEqual([issue.name for issue in outcome.issues], ["uptime-kuma"])
        self.assertIn("bad payload", outcome.issues[0].detail)

    def test_unreachable_docker_is_an_issue(self):
        docker = FakeDocker({})

        def names():
            raise DockerError("connection refused")

        docker.names = names
        outcome = run(docker, False)
        self.assertEqual(outcome.issues[0].name, "docker")

    def test_a_failed_release_check_is_recorded_for_the_main_container(self):
        docker = FakeDocker({
            "adguard": Box("adguard/adguardhome:v0.107.79", "ag", "adguard", "adguard"),
            "opencloud-collabora-1": Box("collabora/code:25.04.1.1", "co", "opencloud", "collabora"),
        })
        with mock.patch.object(jobs, "github_releases", side_effect=OSError("rate limited")), \
                mock.patch.object(jobs, "hub_tags", side_effect=OSError("hub down")):
            outcome = run(docker, False)
        self.assertEqual(outcome.unchecked, [("adguard", "rate limited")])
        self.assertEqual(outcome.issues, [])


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
        def stopped(_docker, _dry, _rejected, outcome):
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
        self.assertEqual(self.run.call_args.args[2], {("traefik", "sha256:new")})

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


if __name__ == "__main__":
    unittest.main()
