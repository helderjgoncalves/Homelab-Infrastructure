"""Policies: what a run bumps, rolls back, retries or leaves alone."""

from __future__ import annotations

import os
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from unittest import mock

from updater import jobs
from updater.jobs import HOLD_NOTE, Outcome, apply_update, run, update_floating, update_semver
from updater.mail import Bump, Notice, pinned_letter
from updater.releases import Release, choose
from updater.runtime import Container, DockerError, Stack
from tests.support import (
    NOW,
    WEEK,
    CONFIG,
    APPS,
    release,
    Box,
    FakeDocker,
    traefik,
    bump,
    bump_traefik,
)


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
        from updater.runtime import Docker

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
        from updater.runtime import Docker

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
        from updater.runtime import Docker

        docker = Docker()
        sequence = iter(Container("beszel", "henrygd/beszel:0", "id", "0", status, health, restarts) for status, health, restarts in states)
        with mock.patch.object(docker, "inspect", side_effect=lambda _name: next(sequence)), mock.patch("updater.runtime.time.sleep"):
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
        from updater.runtime import Docker

        self.docker = Docker(compose_container="dockge")
        patcher = mock.patch("updater.runtime.time.sleep")
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

    def test_a_major_tag_with_no_suffix_reads_the_variant_config_names(self):
        docker = FakeDocker({"mosquitto": Box("eclipse-mosquitto:2", "mq", "zigbee2mqtt", "mosquitto")}, versions={"mq": "2.1.4"})
        self.assertEqual(self.notices(docker, hub=[release("2.2.0", 0)]), [])
        self.hub.assert_called_once_with("library/eclipse-mosquitto", "-alpine")
        notices = self.notices(docker, hub=[release("2.2.0", 0), release("3.0.0", 0)])
        self.assertEqual(notices, [Notice("mosquitto", "2.1.4", "3.0.0", "https://hub.docker.com/_/eclipse-mosquitto/tags")])

    def test_a_digest_pinned_tag_is_read_without_the_digest(self):
        docker = FakeDocker({"zigbee2mqtt": Box("ghcr.io/koenkk/zigbee2mqtt:2.14@sha256:00", "z", "zigbee2mqtt", "zigbee2mqtt")}, versions={"z": "2.14.3"})
        self.assertEqual([notice.available for notice in self.notices(docker, [release("2.15.0", 0)])], ["2.15.0"])

    def test_hub_tags_keep_only_the_variant(self):
        from updater import releases

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
