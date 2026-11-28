"""A compose file's image line changed with nothing else touched."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from updater import composefile


class ComposeFileTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)

    def write(self, name: str, text: str) -> str:
        path = self.dir / name
        path.write_bytes(text.encode())
        return str(path)

    def test_the_next_tag_keeps_the_shape(self):
        self.assertEqual(composefile.next_tag("2026.11", (2026, 12, 1)), "2026.12")
        self.assertEqual(composefile.next_tag("v3", (4, 0, 2)), "v4")
        self.assertEqual(composefile.next_tag("2.1-alpine", (2, 2, 0)), "2.2-alpine")
        self.assertIsNone(composefile.next_tag("latest", (1, 0)))
        self.assertIsNone(composefile.next_tag("1.2.3", (2, 0)))

    def test_retag(self):
        self.assertEqual(composefile.retag("ghcr.io/x/y:2026.11", "2026.12"), "ghcr.io/x/y:2026.12")
        self.assertEqual(composefile.retag("localhost:5000/app", "v2"), "localhost:5000/app:v2")
        with self.assertRaises(composefile.EditError):
            composefile.retag("x/y:1@sha256:00", "2")

    def test_only_the_image_line_changes(self):
        text = "name: x\r\nservices:\r\n  db:\r\n    image: postgres:16\r\n  app:\r\n    # pinned\r\n    image: 'x/app:v3'  # major\r\n    environment:\r\n      image: not-this\r\n"
        path = self.write("compose.yaml", text)
        edits = composefile.plan([path], [("app", "x/app:v3", "x/app:v4")])
        self.assertEqual(edits, [composefile.Edit(path, text, text.replace("'x/app:v3'", "'x/app:v4'"))])
        composefile.write(edits)
        self.assertEqual(Path(path).read_bytes(), text.replace("'x/app:v3'", "'x/app:v4'").encode())
        composefile.revert(edits)
        self.assertEqual(Path(path).read_bytes(), text.encode())

    def test_the_file_that_sets_the_image_is_the_one_edited(self):
        main = self.write("docker-compose.yml", "services:\n  app:\n    image: docker.io/library/nginx:1.27\n")
        override = self.write("docker-compose.override.yml", "services:\n  app:\n    ports:\n      - 80:80\n")
        edits = composefile.plan([main, override], [("app", "nginx:1.27", "nginx:1.28")])
        self.assertEqual([edit.path for edit in edits], [main])
        self.assertIn("docker.io/library/nginx:1.28", edits[0].after)

    def test_what_it_refuses(self):
        cases = [
            ("services:\n  app:\n    image: x:${TAG}\n", "not a plain image:tag"),
            ("services:\n  app:\n    image: x:1@sha256:00\n", "pinned by digest"),
            ("services:\n  app:\n    image: y:1\n", "says y:1 for app, but it runs x:1"),
            ("services:\n  app:\n    build: .\n", "no file of the project sets the image of app"),
            ("services:\n  app:\n    image: >-\n      x:1\n", "not a plain image:tag"),
        ]
        for text, message in cases:
            with self.subTest(message=message):
                path = self.write("docker-compose.yml", text)
                with self.assertRaises(composefile.EditError) as caught:
                    composefile.plan([path], [("app", "x:1", "x:2")])
                self.assertIn(message, str(caught.exception))
        first = self.write("a.yml", "services:\n  app:\n    image: x:1\n")
        second = self.write("b.yml", "services:\n  app:\n    image: x:1\n")
        with self.assertRaises(composefile.EditError):
            composefile.plan([first, second], [("app", "x:1", "x:2")])

    def test_a_service_block(self):
        text = "services:\n  db:\n    image: a\n\n  app:\n    image: b\n    ports:\n      - 1:1\n\nvolumes:\n  data:\n"
        self.assertEqual(composefile.block(text, "app"), "  app:\n    image: b\n    ports:\n      - 1:1\n")
        self.assertEqual(composefile.block(text, "data"), "")

    def test_only_images(self):
        before = {"services": {"app": {"image": "x:1", "environment": {"A": "1"}}}}
        after = {"services": {"app": {"image": "docker.io/x:2", "environment": {"A": "1"}}}}
        self.assertTrue(composefile.only_images(before, after, {"app": "x:2"}))
        self.assertFalse(composefile.only_images(before, after, {"app": "x:3"}))
        changed = {"services": {"app": {"image": "x:2", "environment": {"A": "2"}}}}
        self.assertFalse(composefile.only_images(before, changed, {"app": "x:2"}))
