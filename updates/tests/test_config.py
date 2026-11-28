"""config.toml, its mistakes, and the same decisions as before it."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from updater import config
from updater import jobs
from updater import main as update
from updater.config import ConfigError
from updater.jobs import run
from tests import test_main
from tests.support import (
    CONFIG,
    APPS,
    Box,
    FakeDocker,
)


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
            (self.BASE + app + 'policy = "floating"\ncontainers = ["x"]\nreleases = "github:a/b"\nvariant = "-alpine"\n', "for hub: releases"),
            (self.BASE + app + 'policy = "floating"\ncontainers = ["x"]\nreleases = "hub:a/b"\nvariant = "alpine"\n', "a tag suffix such as -alpine"),
        ]
        for text, message in cases:
            with self.subTest(message=message):
                self.rejects(text, message)

    def test_a_broken_config_is_mailed_and_fails_the_run(self):
        main = test_main.MainTests("test_only_the_weekly_run_prunes")
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
        main = test_main.MainTests("test_only_the_weekly_run_prunes")
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

    def test_immich_and_home_assistant_wait(self):
        self.assertEqual({key: app.wait_days for key, app in APPS.items() if app.wait_days}, {"immich": 7, "homeassistant": 3})

    def test_photos_dns_and_ingress_are_critical(self):
        self.assertEqual({key for key, app in APPS.items() if app.critical}, {"immich", "adguard", "traefik"})

    def test_explain_says_what_each_release_does(self):
        hand = "emailed, installed by hand"
        shadow = "emailed with Workers AI's verdict (shadow), installed by hand"
        asked = "Workers AI is asked: by itself if nothing needs doing by hand, else emailed"
        waited = "Workers AI is asked, 3 days after it is out: by itself if nothing needs doing by hand, else emailed"
        cases = [
            ("homeassistant", "2026.11.3", [("patch", waited), ("minor", waited), ("major", waited)]),
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
        self.assertEqual({key for key, mode in modes.items() if mode == "on"}, {"uptime-kuma", "radarr", "sonarr", "prowlarr", "maintainerr", "seerr", "homeassistant", "homer", "beszel", "dockge"})
        self.assertEqual({key for key, mode in modes.items() if mode == "shadow"}, {"adguard", "zigbee2mqtt"})
        self.assertEqual([key for key, app in APPS.items() if app.critical and app.auto == "on"], [])
        for key in ("immich", "traefik", "opencloud-opencloud-1", "mosquitto", "qbittorrent"):
            self.assertNotIn(key, modes)
        self.assertNotIn("/app/data/configuration.yaml", APPS["zigbee2mqtt"].context_files)
