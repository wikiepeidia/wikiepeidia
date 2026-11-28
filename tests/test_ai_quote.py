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


class TestParsingAndObscureOutputs(unittest.TestCase):
    def test_friendly_model_formatting(self):
        self.assertEqual(aq._friendly_model(""), "Mystery Neural Net")
        self.assertEqual(aq._friendly_model("openai/gpt-oss-120b"), "GPT OSS 120B")
        self.assertEqual(aq._friendly_model("meta-llama/llama-3.3-70b-instruct:free"), "Llama 3.3 70B Instruct")
        self.assertEqual(aq._friendly_model("---"), "---")

    def test_prompt_mentions_codeblock_model_reveal_and_repo_owner(self):
        self.assertIn("{codeblock:", aq.SYSTEM_PROMPT)
        self.assertIn("wikiepeidia", aq.SYSTEM_PROMPT)
        self.assertIn("Code is optional", aq.SYSTEM_PROMPT)

    def test_wrong_and_malformed_json_rejected_asap(self):
        bad_json_outputs = [
            '{"quote": "truncated',
            '{"quote": "trailing comma", "author": "bot",}',
            '{"wrong_key": "hello", "author": "bot"}',
            '[{"quote": "array instead of object", "author": "bot"}]',
            '{"quote": 12345, "author": "bot"}',
            '{"quote": "", "author": "Bot"}',
            'Here is JSON:\n{"quote": "broken", "author": ',
            '{"quote": "unclosed brace"',
        ]
        for raw in bad_json_outputs:
            with self.subTest(raw=raw):
                self.assertIsNone(aq.parse_quote_payload(raw, "openai/gpt-oss-120b"))

    def test_obscure_ai_outputs_think_tags_fences_and_non_string_author(self):
        # Reasoning <think> block followed by fenced JSON
        raw_think = (
            "<think>Hmm let me craft a funny joke about JS...\n"
            'maybe {"quote": "fake"} inside think!</think>\n'
            "```json\n"
            '{"quote": "{codeblock: 1 + \'1\'} is very funny", "author": "Javascript learner"}\n'
            "```"
        )
        self.assertEqual(
            aq.parse_quote_payload(raw_think, "qwen/qwen3:free"),
            ("{codeblock: 1 + '1'} is very funny", "Javascript learner"),
        )

        # Truncated <think> block with no answer -> None
        self.assertIsNone(aq.parse_quote_payload("<think>still thinking forever..."))

        # Non-string author in JSON falls back cleanly to friendly model alias
        self.assertEqual(
            aq.parse_quote_payload('{"quote": "Shipped at 3AM", "author": ["not", "str"]}', "openai/gpt-oss-120b"),
            ("Shipped at 3AM", "I am an AI - GPT OSS 120B"),
        )

        # Plain-text response starting with {codeblock: ...} must NOT be misread as JSON
        self.assertEqual(
            aq.parse_quote_payload('"{codeblock: 1+ \'1\'} is very funny" - Javascript learner'),
            ("{codeblock: 1+ '1'} is very funny", "Javascript learner"),
        )

        # Unicode / Vietnamese accents / smart quotes
        self.assertEqual(
            aq.parse_quote_payload('{"quote": "UPDAFRADE TÉT 💀 — it works™", "author": "Sleepy Bot"}'),
            ("UPDAFRADE TÉT 💀 — it works™", "Sleepy Bot"),
        )

    def test_parse_quote_payload_plain_text_and_empty_inputs(self):
        self.assertIsNone(aq.parse_quote_payload(""))
        self.assertIsNone(aq.parse_quote_payload("   \n  "))
        self.assertIsNone(aq.parse_quote_payload('""'))
        self.assertIsNone(aq.parse_quote_payload("```json\n   \n```"))

        self.assertEqual(
            aq.parse_quote_payload("“Never push on Friday.”\n- Hallucinated by Llama 3.3"),
            ("Never push on Friday.", "Hallucinated by Llama 3.3"),
        )
        self.assertEqual(
            aq.parse_quote_payload("Tabs vs spaces ended in YAML.", "meta-llama/llama-3.3-70b-instruct:free"),
            ("Tabs vs spaces ended in YAML.", "I am an AI - Llama 3.3 70B Instruct"),
        )


class TestCodeblockTokenizationAndSvg(unittest.TestCase):
    def test_tokenize_quote_codeblock_nested_braces_backticks_and_unclosed(self):
        # User's exact example
        self.assertEqual(
            aq.tokenize_quote("{codeblock: 1+ '1'} is very funny"),
            [(True, "1+ '1'"), (False, " is very funny")],
        )
        # Nested braces inside JS arrow function & object literal
        self.assertEqual(
            aq.tokenize_quote("Try {codeblock: () => { return {a: 1}; }} today"),
            [(False, "Try "), (True, "() => { return {a: 1}; }"), (False, " today")],
        )
        # Mixed backticks and {codeblock: ...}
        self.assertEqual(
            aq.tokenize_quote("`rm -rf` vs {codeblock: git reset --hard}"),
            [(True, "rm -rf"), (False, " vs "), (True, "git reset --hard")],
        )
        # Unclosed {codeblock: and unclosed backtick degrade safely to plain text
        self.assertEqual(
            aq.tokenize_quote("{codeblock: unclosed and `backtick"),
            [(False, "{codeblock: unclosed and `backtick")],
        )
        # Empty text fallback
        self.assertEqual(aq.tokenize_quote(""), [(False, "")])

    def test_render_quote_svg_formats_codeblock_and_escapes_xss(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "images" / "ai-quote.svg"
            aq.render_quote_svg("{codeblock: 1+ '1' & <script>} is very funny", "Javascript learner", out)
            root = _parse_svg(out)
            svg_text = out.read_text(encoding="utf8")

            self.assertNotIn("{codeblock:", svg_text)
            self.assertNotIn("<script>", svg_text)
            self.assertIn('<tspan class="code">1+ &#x27;1&#x27; &amp; &lt;script&gt;</tspan>', svg_text)
            self.assertIn("- Javascript learner", svg_text)
            self.assertEqual(root.find("{http://www.w3.org/2000/svg}title").text, "1+ '1' & <script> is very funny — Javascript learner")

    def test_render_quote_svg_wraps_giant_codeblock_and_long_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "images" / "ai-quote.svg"
            huge_code = "{codeblock: " + ("x" * 150) + "}"
            aq.render_quote_svg(huge_code + " " + ("word " * 50), "Bot " + ("Y" * 100), out)
            root = _parse_svg(out)
            svg_text = out.read_text(encoding="utf8")
            self.assertIn('<tspan class="code">', svg_text)
            self.assertIn("…", svg_text)
            self.assertLessEqual(int(root.attrib["height"]), 220)


class TestProviderCallsAndFailover(unittest.TestCase):
    def test_call_provider_without_extra_payload(self):
        with patch.object(aq, "urlopen", return_value=_FakeResponse({
            "model": "custom/model",
            "choices": [{"message": {"content": '{"quote": "Minimal payload works", "author": "Bot"}'}}],
        })):
            self.assertEqual(aq.call_provider("groq", aq.GROQ_URL, "k", "m"), ("Minimal payload works", "Bot"))

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
            quote, author, from_api = aq.fetch_ai_quote()

        self.assertTrue(from_api)
        self.assertEqual((quote, author), ("My weights are heavy.", "I am an AI - GPT OSS 120B"))
        self.assertEqual(captured["url"], aq.GROQ_URL)
        self.assertEqual(captured["auth"], "Bearer gsk_test_key")
        self.assertTrue(captured["ua"].startswith("wikiepeidia-"))
        self.assertIn(captured["body"]["model"], dict(aq.GROQ_MODELS))
        for key, value in dict(aq.GROQ_MODELS)[captured["body"]["model"]].items():
            self.assertEqual(captured["body"][key], value)
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
            quote, author, from_api = aq.fetch_ai_quote()

        self.assertTrue(from_api)
        self.assertEqual(quote, "Free tokens taste sweetest.")
        self.assertEqual(author, "I am an AI - Gemma 3 27B It")
        self.assertEqual(captured["url"], aq.OPENROUTER_URL)
        self.assertEqual(captured["auth"], "Bearer sk-or-test")
        self.assertIn("github.com", captured["referer"])
        self.assertEqual(captured["body"]["model"], "openrouter/free")

    def test_wrong_json_triggers_immediate_failover_then_fallback(self):
        """Wrong JSON from first provider fails over ASAP; if all return wrong JSON -> from_api=False."""
        class _NoShuffleRng(random.Random):
            def shuffle(self, x):
                x.sort(key=lambda item: item[0])

        bad_json_resp = _FakeResponse({
            "model": "openai/gpt-oss-120b",
            "choices": [{"message": {"content": '{"quote": "broken json missing brace"'}}],
        })
        good_openrouter = _FakeResponse({
            "model": "qwen/qwen3-coder:free",
            "choices": [{"message": {"content": '{"quote": "{codeblock: 1 + \'1\'} works", "author": "Qwen 3 Coder"}'}}],
        })

        with patch.dict(os.environ, {"GROK_API_KEY": "gsk_1", "OPENROUTER_API_KEY": "sk-or-2"}), \
             patch.object(aq, "urlopen", side_effect=[bad_json_resp, good_openrouter]), \
             contextlib.redirect_stdout(io.StringIO()):
            quote, author, from_api = aq.fetch_ai_quote(rng=_NoShuffleRng(0))
        self.assertTrue(from_api)
        self.assertEqual((quote, author), ("{codeblock: 1 + '1'} works", "Qwen 3 Coder"))

        # Now ALL providers output wrong JSON -> must set from_api=False so workflow keeps old image
        all_bad = [
            _FakeResponse({"choices": [{"message": {"content": '{"wrong": "shape"}'}}]}),
            _FakeResponse({"choices": [{"message": {"content": '{"quote": "truncated...'}}]}),
            _FakeResponse({"choices": [{"message": {"content": "<think>cutoff"}}]}),
        ]
        with patch.dict(os.environ, {"GROK_API_KEY": "gsk_1", "OPENROUTER_API_KEY": "sk-or-2"}), \
             patch.object(aq, "urlopen", side_effect=all_bad), \
             contextlib.redirect_stdout(io.StringIO()):
            quote, author, from_api = aq.fetch_ai_quote(rng=random.Random(0))
        self.assertFalse(from_api)
        self.assertIn((quote, author), aq.FALLBACK_QUOTES)

    def test_failover_when_first_provider_returns_401(self):
        secret1 = "gsk_expired_secret_12345"
        secret2 = "sk-or-working-67890"
        err401 = HTTPError(aq.GROQ_URL, 401, "Unauthorized", Message(), None)
        ok_openrouter = _FakeResponse({
            "model": "qwen/qwen3-coder:free",
            "choices": [{"message": {"content": '{"quote": "Failover saved my README.", "author": "Qwen 3 Coder"}'}}],
        })

        class _NoShuffleRng(random.Random):
            def shuffle(self, x):
                x.sort(key=lambda item: item[0])

        buf = io.StringIO()
        with patch.dict(os.environ, {"GROK_API_KEY": secret1, "OPENROUTER_API_KEY": secret2}), \
             patch.object(aq, "urlopen", side_effect=[err401, ok_openrouter]), \
             contextlib.redirect_stdout(buf):
            quote, author, from_api = aq.fetch_ai_quote(rng=_NoShuffleRng(0))

        self.assertTrue(from_api)
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
            buf = io.StringIO()
            with patch.dict(os.environ, {"GROK_API_KEY": secret1, "OPENROUTER_API_KEY": secret2}), \
                 patch.object(aq, "urlopen", side_effect=[bad] * 3), \
                 contextlib.redirect_stdout(buf):
                quote, author, from_api = aq.fetch_ai_quote(rng=random.Random(0))
            self.assertFalse(from_api)
            self.assertIn((quote, author), aq.FALLBACK_QUOTES)
            self.assertNotIn(secret1, buf.getvalue())
            self.assertNotIn(secret2, buf.getvalue())

    def test_repeat_with_inline_code_and_new_byline_tries_next_model(self):
        old = "The logger wrote its own resignation letter."
        with patch.dict(os.environ, {"GROQ_API_KEY": "test"}, clear=True), patch.object(
            aq, "call_provider", side_effect=[("The {codeblock: logger} wrote its own resignation letter!", "New"),
                                             ("The clipboard still has my first draft.", "Clipboard")]
        ) as provider:
            self.assertEqual(aq.fetch_ai_quote(history=[old])[0], "The clipboard still has my first draft.")
            self.assertEqual(provider.call_count, 2)

    def test_missing_or_whitespace_secrets_use_fallback_quote(self):
        with patch.dict(os.environ, {"GROK_API_KEY": "   ", "OPENROUTER_API_KEY": ""}, clear=False), \
             contextlib.redirect_stdout(io.StringIO()):
            os.environ.pop("GROQ_API_KEY", None)
            quote, author, from_api = aq.fetch_ai_quote(rng=random.Random(1))
        self.assertFalse(from_api)
        self.assertIn((quote, author), aq.FALLBACK_QUOTES)


class TestMainIntegration(unittest.TestCase):
    def test_main_renders_ai_quote_svg_and_sets_github_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            gh_out = Path(tmp) / "gh_output.txt"
            try:
                with patch.dict(os.environ, {"GITHUB_OUTPUT": str(gh_out)}), \
                     patch.object(aq, "fetch_ai_quote", return_value=("404 Sleep Not Found", "I am an AI - GPT OSS 120B", True)):
                    aq.main()
                svg_path = Path("images/ai-quote.svg")
                self.assertTrue(svg_path.exists())
                _parse_svg(svg_path)
                svg_text = svg_path.read_text(encoding="utf8")
                self.assertIn("404 Sleep Not Found", svg_text)
                self.assertIn("- I am an AI - GPT OSS 120B", svg_text)
                self.assertEqual(gh_out.read_text(encoding="utf8").strip(), "from_api=true")

                gh_out.write_text("", encoding="utf8")
                with patch.dict(os.environ, {"GITHUB_OUTPUT": str(gh_out)}), \
                     patch.object(aq, "fetch_ai_quote", return_value=("{codeblock: 1 + '1'}", "Bot", False)):
                    aq.main()
                self.assertEqual(gh_out.read_text(encoding="utf8").strip(), "from_api=false")
                self.assertIn('<tspan class="code">1 + &#x27;1&#x27;</tspan>', svg_path.read_text(encoding="utf8"))

                # Run without GITHUB_OUTPUT set
                with patch.dict(os.environ, {}, clear=False), \
                     patch.object(aq, "fetch_ai_quote", return_value=("Local run", "Bot", True)):
                    os.environ.pop("GITHUB_OUTPUT", None)
                    aq.main()
            finally:
                os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
