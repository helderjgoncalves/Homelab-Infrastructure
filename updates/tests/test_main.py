"""The run itself: state files, the weekly and failure mail, the lock and the pushes."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from updater import jobs
from updater import mail
from updater import push
from updater import main as update
from updater.jobs import Outcome
from updater.mail import Bump, Issue, Notice
from updater.releases import Release
from updater.main import Kept, digest_due, merge_bumps
from tests.support import (
    SUNDAY,
    CONFIG,
    APPS,
    release,
    StateTests,
    MainCase,
)


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
