"""The Workers AI request and its answer, and what it was shown."""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path
from unittest import mock

from updater import auto
from updater import notes
from updater import verdict
from updater.verdict import Verdict, VerdictError
from tests.support import (
    FakeDocker,
)


class VerdictTests(unittest.TestCase):
    ENV = {"CLOUDFLARE_ACCOUNT_ID": "acct", "CLOUDFLARE_AI_TOKEN": "token"}
    QUESTION = verdict.Question("Traefik", "3.6.2", "4.0.1", "traefik traefik:v3 → traefik:v4", "  traefik:\n    image: traefik:v3", notes="- Fixes.")

    def test_parse(self):
        found = verdict.parse('Here:\n```json\n{"decision": "auto", "reasons": ["a", "b", "c", "d"], "actions_needed": []}\n```')
        self.assertEqual(found, Verdict("auto", ("a", "b", "c")))
        self.assertTrue(found.auto)
        self.assertFalse(Verdict("auto", (), ("Edit a file.",)).auto)
        self.assertEqual(verdict.parse('{"decision": "manual", "reasons": ["a"]}'), Verdict("manual", ("a",)))
        for text in ("no json", '{"decision": "maybe"}', '{"decision": "auto", "reasons": "a"}', '{"decision": "manual", "actions_needed": [1]}',
                     '{"decision": "auto", "reasons": ["a"], "actions": ["Run the migration."]}'):
            with self.subTest(text=text), self.assertRaises(VerdictError):
                verdict.parse(text)

    def test_the_request(self):
        sent = []

        class Reply(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        def urlopen(request, timeout):
            sent.append((request, timeout))
            answer = {"decision": "manual", "reasons": ["A flag was renamed."], "actions_needed": ["Rename it."]}
            return Reply(json.dumps({"choices": [{"message": {"content": json.dumps(answer)}}]}).encode())

        with mock.patch.object(verdict.urllib.request, "urlopen", urlopen):
            found = verdict.ask(self.QUESTION, self.ENV)
        self.assertEqual(found, Verdict("manual", ("A flag was renamed.",), ("Rename it.",)))
        self.assertEqual(json.loads(found.said)["actions_needed"], ["Rename it."])
        request, timeout = sent[0]
        self.assertEqual(request.full_url, "https://api.cloudflare.com/client/v4/accounts/acct/ai/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer token")
        body = json.loads(request.data)
        self.assertEqual((body["model"], body["max_tokens"], body["temperature"]), ("@cf/openai/gpt-oss-120b", 4096, 0))
        self.assertEqual(body["messages"][0], {"role": "system", "content": verdict.SYSTEM})
        self.assertIn("<notes>\n- Fixes.\n</notes>", body["messages"][1]["content"])
        self.assertEqual(timeout, 180)

    def test_errors(self):
        def refused(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 429, "Too Many Requests", {}, io.BytesIO(b"daily limit"))

        def failing(code: int, body: bytes):
            def urlopen(request, timeout):
                raise urllib.error.HTTPError(request.full_url, code, "", {}, io.BytesIO(body))
            return urlopen

        with mock.patch.object(verdict.urllib.request, "urlopen", refused), self.assertRaises(verdict.Unavailable) as caught:
            verdict.ask(self.QUESTION, self.ENV)
        self.assertEqual(str(caught.exception), "it has no tokens left today (HTTP 429)")
        for code, body, said in (
            (400, b'{"errors": [{"code": 4006, "message": "used up your daily free allocation"}]}', "it has no tokens left today (HTTP 400)"),
            (401, b"bad token", "it refused the token (HTTP 401)"),
        ):
            with mock.patch.object(verdict.urllib.request, "urlopen", failing(code, body)), self.assertRaises(verdict.Unavailable) as caught:
                verdict.ask(self.QUESTION, self.ENV)
            self.assertEqual(str(caught.exception), said)
        with mock.patch.object(verdict.urllib.request, "urlopen", failing(500, b"busy")), self.assertRaises(VerdictError) as caught:
            verdict.ask(self.QUESTION, self.ENV)
        self.assertNotIsInstance(caught.exception, verdict.Unavailable)
        self.assertIn("HTTP 500: busy", str(caught.exception))
        with self.assertRaises(VerdictError):
            verdict._content({"choices": [{"message": {"content": ""}}]})
        self.assertEqual(verdict._content({"result": {"response": "{}"}}), "{}")

    def test_configured(self):
        self.assertTrue(verdict.configured(self.ENV))
        self.assertFalse(verdict.configured({"CLOUDFLARE_ACCOUNT_ID": "acct"}))


class ShownTextTests(unittest.TestCase):
    def test_an_unclosed_item_in_a_skipped_menu_keeps_the_rest_of_the_page(self):
        page = '<nav><ul><li>Home<li>Blog</ul></nav><h2>Breaking changes</h2><p>X was removed.</p>'
        self.assertEqual(notes.page_text(page), "## Breaking changes\n\nX was removed.")
        self.assertEqual(notes.page_text('<nav><nav>a</nav>b</nav><p>c<span class="tooltip">d<span>e</span></span> f</p>'), "c f")

    def test_cant_be_undone_is_found_quickly_with_its_sentence(self):
        started = time.monotonic()
        self.assertEqual(notes.undone("word " * 20000 + "so it cannot be undone"), ["word " * 17 + "word so it cannot be undone"])
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(notes.undone("Fixes. Downgrading is not supported, sorry.\nIt can't be undone, it is irreversible."),
                         ["Downgrading is not supported, sorry", "It can't be undone, it is irreversible"])

    def test_trailing_compose_comments_are_dropped(self):
        self.assertEqual(auto._uncommented('    image: "traefik:v3"   # after a major, run the tool'), '    image: "traefik:v3"')
        for line in ("      - 'a # b'", "      URL: http://x/#frag", '    command: ["--x=#1"]'):
            self.assertEqual(auto._uncommented(line), line)

    def test_secret_values_in_files_shown_are_hidden(self):
        text = "- trigger: webhook\n  webhook_id: abc123\n  api_key: !secret k\n  entity_id: cover.curtains\n"
        self.assertEqual(auto.hide_secrets(text), "- trigger: webhook\n  webhook_id: (hidden)\n  api_key: (hidden)\n  entity_id: cover.curtains\n")
        text = ("rest_command:\n  open:\n    headers:\n      authorization: \"Bearer abc\"\n    private_key: |\n      line one\n\n      line two\n"
                "    url: http://x\n- password: >-\n    folded\n  name: kept\n")
        self.assertEqual(auto.hide_secrets(text), "rest_command:\n  open:\n    headers:\n      authorization: (hidden)\n    private_key: (hidden)\n"
                                                  "    url: http://x\n- password: (hidden)\n  name: kept\n")

    def test_the_inventory_takes_each_entry_s_own_name_only(self):
        entries = {"data": {"entries": [{"domain": "duckdns", "data": {"domain": "home.duckdns.org"}}, {"domain": "mqtt", "discovery_keys": {"x": [{"domain": "zeroconf"}]}}]}}
        self.assertEqual(sorted(set(auto._values(entries, "domain"))), ["duckdns", "mqtt"])

    def test_the_journal_keeps_a_year(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder)
        for name in ("2025-10", "2025-11", "2026-10"):
            (folder / f"{name}.jsonl").write_text("{}\n")
        auto.Judge(FakeDocker({}), {}, {}, today=date(2026, 11, 27), journal=folder)._journal({"app": "x"})
        self.assertEqual(sorted(path.stem for path in folder.glob("*.jsonl")), ["2025-11", "2026-10", "2026-11"])

    def test_an_answer_with_no_message_is_no_answer(self):
        with self.assertRaises(VerdictError):
            verdict._content({"choices": [{"message": "text"}]})
