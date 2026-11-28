"""Workers AI moves: which releases are asked about, and what follows from the answer."""

from __future__ import annotations

import io
import json
import os
import re
import stat
import unittest
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from datetime import date, timedelta
from unittest import mock

from updater import auto
from updater import jobs
from updater import notes
from updater import push
from updater import runtime
from updater import main as update
from updater import verdict
from updater.verdict import HELD_AI, HELD_BREAKING, Verdict, VerdictError
from updater.jobs import Outcome, update_floating, update_semver
from updater.mail import Bump, Issue, Notice, pinned_letter
from updater.runtime import DockerError
from updater.main import Kept, merge_bumps
from tests import test_verdict
from tests.support import (
    SUNDAY,
    CONFIG,
    APPS,
    Box,
    FakeDocker,
    StateTests,
    FileDocker,
    noted,
    AutoCase,
    TRAEFIK_COMPOSE,
    TRAEFIK_URL,
)


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
        self.app = replace(APPS["homeassistant"], auto="on", rollback_files=(), wait_days=0)
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
        self.app = replace(self.app, wait_days=APPS["homeassistant"].wait_days)
        self.config = replace(CONFIG, apps={**APPS, "homeassistant": self.app})
        self.releases = [noted("2026.11.3", 40), noted("2026.11.4", 2, "- Fix a thing.")]
        self.assertEqual((self.collect("").bumps, self.asked), ([], []))
        self.releases[1] = noted("2026.11.4", 3, "- Fix a thing.")
        outcome = self.collect("")
        self.assertEqual(len(self.asked), 1)
        self.assertEqual([(bump.before, bump.after) for bump in outcome.bumps], [("2026.11.3", "2026.11.4")])
        self.assertIn("home-assistant:2026.11.4\n", path.read_text())


class RunnerAutoTests(AutoCase):
    """Dockge runs every `docker compose`, so a major it takes by itself recreates it last, and saves no files: that would stop it."""

    def test_a_major_of_the_compose_runner_moves_its_tag_and_recreates_it(self):
        path = self.compose_file("dockge", "services:\n  dockge:\n    image: louislam/dockge:1\n")
        docker = FileDocker({"dockge": Box("louislam/dockge:1", "old", "dockge", "dockge", (str(path),))}, {"louislam/dockge:2": "new"})
        docker.paths[("dockge", "/app/package.json")] = b'{"version": "1.5.0"}'
        app = APPS["dockge"]
        self.assertEqual((app.auto, app.rollback_files), ("on", ()))
        config = replace(CONFIG, apps={"dockge": app})
        outcome = Outcome()
        releases = [noted("1.5.0", 90), noted("2.0.0", 5, "- New look.")]
        with mock.patch.object(jobs, "github_releases", return_value=releases):
            jobs.collect_outside(docker, config, set(docker.boxes), outcome.notices, None, self.judge(docker), jobs.Morning(docker, config, False, outcome))
        self.assertEqual(len(self.asked), 1)
        self.assertIn("image: louislam/dockge:2\n", path.read_text())
        self.assertIn(("recreate", "dockge"), docker.calls)
        self.assertEqual(docker.boxes["dockge"].image_id, "new")
        self.assertNotIn(("compose", "dockge", "stop", "dockge"), [call[:4] for call in docker.calls])

    def test_the_compose_runner_is_checked_past_its_tag_last(self):
        seen = []
        config = replace(CONFIG, apps={"dockge": APPS["dockge"], "homer": APPS["homer"]})
        with mock.patch.object(jobs, "_outside_notice", lambda _docker, _notices, app, *_args: seen.append(app.key)):
            jobs.collect_outside(FakeDocker({}), config, {"dockge", "homer"}, [])
        self.assertEqual(seen, ["homer", "dockge"])


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
        self.app = replace(APPS["homeassistant"], auto="on", linked_notes=False, rollback_files=(), breaking_marker="(breaking-change)", breaking_names="[{} docs]",
                           wait_days=0)

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

class JudgeRuleTests(unittest.TestCase):
    def test_code_can_only_say_no(self):
        said = Verdict("auto", ("Only fixes.",))
        self.assertEqual(auto.combine(said, []), said)
        self.assertEqual(auto.combine(said, ["Names mqtt."]), Verdict("manual", ("Names mqtt.", "Only fixes.")))
        manual = Verdict("manual", ("A step.",), ("Do it.",))
        self.assertEqual(auto.combine(manual, []), manual)

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
        with mock.patch.object(update, "_read_env", return_value=test_verdict.VerdictTests.ENV), mock.patch.object(update, "_load_verdicts", side_effect=PermissionError("verdicts.tsv")):
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
