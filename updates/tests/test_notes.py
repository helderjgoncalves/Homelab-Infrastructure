"""Release notes, trimmed to breaking changes."""

from __future__ import annotations

import unittest
from datetime import timedelta
from unittest import mock

from updater import notes
from updater.releases import Release
from tests.support import (
    NOW,
    release,
)


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
        from updater import releases

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
