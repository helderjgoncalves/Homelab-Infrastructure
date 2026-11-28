"""The card mail."""

from __future__ import annotations

import unittest
import urllib.error
import urllib.parse
import urllib.request

from updater.mail import Bump, Issue, Notice, failure_letter, pinned_letter
from updater.releases import newer, parse_version
from updater.runtime import api_prefix, unused_image_prune_path


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
