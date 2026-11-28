"""Compose through the files on the container labels."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from updater import jobs
from updater.jobs import Outcome, run
from updater.runtime import DockerError, Stack, stack_of
from tests import test_jobs
from tests.support import (
    CONFIG,
    Box,
    FakeDocker,
    bump,
    bump_traefik,
)


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
        from updater.runtime import Docker

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
        from updater.runtime import Docker

        docker = Docker()
        payload = {"Id": "sha256:new", "Config": {"Labels": {"org.opencontainers.image.version": "7.0.3.10918-ls330", "build_version": "x"}}}
        with mock.patch.object(docker, "_request", return_value=payload) as request:
            self.assertEqual(docker.image_version("lscr.io/linuxserver/radarr:latest"), "7.0.3.10918-ls330")
        self.assertEqual(request.call_args.args, ("GET", "/images/lscr.io%2Flinuxserver%2Fradarr%3Alatest/json"))
        for bare in ({"Id": "sha256:x", "Config": {"Labels": None}}, {"Id": "sha256:x", "Config": {}}, {"Id": "sha256:x"}):
            with mock.patch.object(docker, "_request", return_value=bare):
                self.assertEqual(docker.image_version("x:1"), "")

    def test_compose_passes_every_file(self):
        from updater.runtime import Docker

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
        from updater.runtime import Docker

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
        test = test_jobs.ImmichTests("test_patch_pulls_the_exact_tag_and_retags_v3")
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
