"""Comprehensive unit tests for .github/scripts/ai_quote.py."""

import contextlib
import io
import json
import os
import random
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / ".github" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import ai_quote as aq


def _parse_svg(path: Path) -> ET.Element:
    return ET.fromstring(path.read_text(encoding="utf8"))


class _FakeResponse:
    def __init__(self, payload, raw_bytes=None):
        data = raw_bytes if raw_bytes is not None else json.dumps(payload).encode("utf8")
        self._buf = io.BytesIO(data)

    def read(self, *args):
        return self._buf.read(*args)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


class TestParsingAndFormatting(unittest.TestCase):
    def test_friendly_model_formatting(self):
        self.assertEqual(aq._friendly_model(""), "Mystery Neural Net")
        self.assertEqual(aq._friendly_model("openai/gpt-oss-120b"), "GPT OSS 120B")
        self.assertEqual(aq._friendly_model("meta-llama/llama-3.3-70b-instruct:free"), "Llama 3.3 70B Instruct")
        self.assertEqual(aq._friendly_model("---"), "---")

    def test_parse_quote_payload_json_fenced_and_missing_author(self):
        # Clean JSON
        self.assertEqual(
            aq.parse_quote_payload('{"quote": "\\"Works on my machine\\"", "author": "I am an AI - GPT OSS 120B"}'),
            ("Works on my machine", "I am an AI - GPT OSS 120B"),
        )
        # Markdown-fenced JSON with leading chatter and empty author -> falls back to friendly model name
        raw = 'Here you go:\n```json\n{"quote": "Sleep is for synchronous code.", "author": ""}\n```'
        self.assertEqual(
            aq.parse_quote_payload(raw, "openai/gpt-oss-120b"),
            ("Sleep is for synchronous code.", "I am an AI - GPT OSS 120B"),
        )
        # Invalid JSON inside braces falls through to plain-text parsing
        self.assertEqual(
            aq.parse_quote_payload('{bad json} — Broken Linter'),
            ("{bad json}", "Broken Linter"),
        )

    def test_parse_quote_payload_plain_text_and_empty_inputs(self):
        self.assertIsNone(aq.parse_quote_payload(""))
        self.assertIsNone(aq.parse_quote_payload("   \n  "))
        self.assertIsNone(aq.parse_quote_payload('{"quote": "", "author": "Bot"}'))
        self.assertIsNone(aq.parse_quote_payload('""'))
        self.assertIsNone(aq.parse_quote_payload('```json\n   \n```'))

        # Two-line quote + dash author
        self.assertEqual(
            aq.parse_quote_payload("“Never push on Friday.”\n- Hallucinated by Llama 3.3"),
            ("Never push on Friday.", "Hallucinated by Llama 3.3"),
        )
        # Single-line quote without author -> synthesizes author from model
        self.assertEqual(
            aq.parse_quote_payload("Tabs vs spaces ended in YAML.", "meta-llama/llama-3.3-70b-instruct:free"),
            ("Tabs vs spaces ended in YAML.", "I am an AI - Llama 3.3 70B Instruct"),
        )


class TestProviderCallsAndFailover(unittest.TestCase):
    def test_groq_uses_grok_secret_and_gpt_oss_120b_with_user_agent(self):
        captured = {}

        def fake_urlopen(req, timeout=0):
            captured["url"] = req.full_url
            captured["auth"] = req.get_header("Authorization")
            captured["ua"] = req.get_header("User-agent")
            captured["body"] = json.loads(req.data.decode("utf8"))
            captured["timeout"] = timeout
            return _FakeResponse({
                "model": "openai/gpt-oss-120b",
                "choices": [{"message": {"content": '{"quote": "My weights are heavy.", "author": "I am an AI - GPT OSS 120B"}'}}],
            })

        with patch.dict(os.environ, {"GROK_API_KEY": "gsk_test_key"}, clear=False), \
             patch.object(aq, "urlopen", side_effect=fake_urlopen):
            os.environ.pop("OPENROUTER_API_KEY", None)
            quote, author = aq.fetch_ai_quote()

        self.assertEqual((quote, author), ("My weights are heavy.", "I am an AI - GPT OSS 120B"))
        self.assertEqual(captured["url"], aq.GROQ_URL)
        self.assertEqual(captured["auth"], "Bearer gsk_test_key")
        self.assertTrue(captured["ua"].startswith("wikiepeidia-"))
        self.assertEqual(captured["body"]["model"], "openai/gpt-oss-120b")
        self.assertEqual(captured["body"]["reasoning_effort"], "low")
        self.assertEqual(captured["timeout"], 25)

    def test_openrouter_free_headers_and_model_reveal(self):
        captured = {}

        def fake_urlopen(req, timeout=0):
            captured["url"] = req.full_url
            captured["auth"] = req.get_header("Authorization")
            captured["referer"] = req.get_header("Http-referer")
            captured["title"] = req.get_header("X-title")
            captured["body"] = json.loads(req.data.decode("utf8"))
            return _FakeResponse({
                "model": "google/gemma-3-27b-it:free",
                "choices": [{"message": {"content": '{"quote": "Free tokens taste sweetest.", "author": ""}'}}],
            })

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test"}, clear=False), \
             patch.object(aq, "urlopen", side_effect=fake_urlopen):
            os.environ.pop("GROK_API_KEY", None)
            os.environ.pop("GROQ_API_KEY", None)
            quote, author = aq.fetch_ai_quote()

        self.assertEqual(quote, "Free tokens taste sweetest.")
        self.assertEqual(author, "I am an AI - Gemma 3 27B It")
        self.assertEqual(captured["url"], aq.OPENROUTER_URL)
        self.assertEqual(captured["auth"], "Bearer sk-or-test")
        self.assertIn("github.com", captured["referer"])
        self.assertEqual(captured["body"]["model"], "openrouter/free")

    def test_failover_when_first_provider_returns_401_or_bad_payload(self):
        secret1 = "gsk_expired_secret_12345"
        secret2 = "sk-or-working-67890"
        err401 = HTTPError(aq.GROQ_URL, 401, "Unauthorized", Message(), None)
        ok_openrouter = _FakeResponse({
            "model": "qwen/qwen3-coder:free",
            "choices": [{"message": {"content": '{"quote": "Failover saved my README.", "author": "Qwen 3 Coder"}'}}],
        })

        # Force deterministic order so first candidate runs first
        class _NoShuffleRng(random.Random):
            def shuffle(self, x):
                x.sort(key=lambda item: item[0])  # "groq" before "openrouter"

        buf = io.StringIO()
        with patch.dict(os.environ, {"GROK_API_KEY": secret1, "OPENROUTER_API_KEY": secret2}), \
             patch.object(aq, "urlopen", side_effect=[err401, ok_openrouter]), \
             contextlib.redirect_stdout(buf):
            quote, author = aq.fetch_ai_quote(rng=_NoShuffleRng(0))

        self.assertEqual((quote, author), ("Failover saved my README.", "Qwen 3 Coder"))
        self.assertIn("HTTP 401", buf.getvalue())
        self.assertNotIn(secret1, buf.getvalue())
        self.assertNotIn(secret2, buf.getvalue())

    def test_all_failure_modes_degrade_safely_without_leaking_secrets(self):
        secret1 = "gsk_never_leak_groq_key"
        secret2 = "sk-or-never-leak-openrouter-key"
        bad_responses = [
            _FakeResponse(["not-a-dict"]),
            _FakeResponse({"choices": []}),
            _FakeResponse({"choices": [{"message": "not-a-dict"}]}),
            _FakeResponse({"choices": [{"message": {"content": None}}]}),
            HTTPError(aq.GROQ_URL, 429, "Rate Limited", Message(), None),
            URLError("offline"),
            TimeoutError("timeout"),
        ]
        for bad in bad_responses:
            effect = [bad] if isinstance(bad, Exception) else [bad]
            buf = io.StringIO()
            with patch.dict(os.environ, {"GROK_API_KEY": secret1, "OPENROUTER_API_KEY": secret2}), \
                 patch.object(aq, "urlopen", side_effect=effect * 3), \
                 contextlib.redirect_stdout(buf):
                quote, author = aq.fetch_ai_quote(rng=random.Random(0))
            self.assertIn((quote, author), aq.FALLBACK_QUOTES)
            self.assertNotIn(secret1, buf.getvalue())
            self.assertNotIn(secret2, buf.getvalue())

    def test_missing_or_whitespace_secrets_use_fallback_quote(self):
        with patch.dict(os.environ, {"GROK_API_KEY": "   ", "OPENROUTER_API_KEY": ""}, clear=False), \
             contextlib.redirect_stdout(io.StringIO()):
            os.environ.pop("GROQ_API_KEY", None)
            quote, author = aq.fetch_ai_quote(rng=random.Random(1))
        self.assertIn((quote, author), aq.FALLBACK_QUOTES)


class TestSvgRenderingAndMain(unittest.TestCase):
    def test_render_quote_svg_escapes_xss_wraps_long_text_and_caps_author(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "images" / "ai-quote.svg"
            evil_quote = '<script>alert("xss")</script> & ' + ("word " * 80)
            evil_author = "I am an AI & <Admin> " + ("X" * 100)
            aq.render_quote_svg(evil_quote, evil_author, out)

            root = _parse_svg(out)
            svg_text = out.read_text(encoding="utf8")
            self.assertNotIn("<script>", svg_text)
            self.assertIn("&lt;script&gt;", svg_text)
            self.assertIn("&amp;", svg_text)
            self.assertIn("…", svg_text)
            # Capped length ensures bounded height even for huge AI output
            self.assertLessEqual(int(root.attrib["height"]), 220)

    def test_main_renders_ai_quote_svg_in_images_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                with patch.object(aq, "fetch_ai_quote", return_value=("404 Sleep Not Found", "I am an AI - GPT OSS 120B")):
                    aq.main()
                svg_path = Path("images/ai-quote.svg")
                self.assertTrue(svg_path.exists())
                _parse_svg(svg_path)
                svg_text = svg_path.read_text(encoding="utf8")
                self.assertIn("404 Sleep Not Found", svg_text)
                self.assertIn("- I am an AI - GPT OSS 120B", svg_text)
            finally:
                os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
