"""QTS Live Update."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from updater import jobs
from updater.mail import Notice, pinned_letter
from updater.releases import Firmware, qts_firmware
from tests.support import (
    APPS,
)


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
