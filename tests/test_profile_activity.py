"""Comprehensive unit tests for .github/scripts/profile_activity.py."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / ".github" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import profile_activity as pa


def _parse_svg(path: Path) -> ET.Element:
    """Assert the SVG is well-formed XML and return its root element."""
    return ET.fromstring(path.read_text(encoding="utf8"))


class _FakeHTTPResponse:
    def __init__(self, body_bytes: bytes):
        self._buf = io.BytesIO(body_bytes)

    def read(self, *args):
        return self._buf.read(*args)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


def _json_response(obj) -> _FakeHTTPResponse:
    return _FakeHTTPResponse(json.dumps(obj).encode("utf8"))


class TestFormattingAndRank(unittest.TestCase):
    def test_clipped_boundaries(self):
        self.assertEqual(pa.clipped("", 24), "")
        self.assertEqual(pa.clipped("a" * 24, 24), "a" * 24)
        self.assertEqual(pa.clipped("a" * 25, 24), "a" * 23 + "…")

    def test_compact_number_normal_extreme_and_non_finite(self):
        self.assertEqual(pa.compact_number(0), "0")
        self.assertEqual(pa.compact_number(1234.6), "1,235")
        self.assertEqual(pa.compact_number(999_999_999), "999,999,999")
        self.assertEqual(pa.compact_number(1_000_000_000), "1.0B")
        self.assertEqual(pa.compact_number(999_000_000_000_000), "999.0T")
        self.assertEqual(pa.compact_number(5_000_000_000_000_000), "5.0Q")
        self.assertEqual(pa.compact_number(2_500_000, threshold=1_000_000), "2.5M")
        self.assertEqual(pa.compact_number(1_500, threshold=1_000), "1.5K")
        # Threshold < 1000 falls through scale loop cleanly
        self.assertEqual(pa.compact_number(500, threshold=100), "500")
        self.assertEqual(pa.compact_number(500.4, threshold=100), "500")
        # Non-finite floats never render "nan" or "inf"
        self.assertEqual(pa.compact_number(float("nan")), "0")
        self.assertEqual(pa.compact_number(float("inf")), "0")
        self.assertEqual(pa.compact_number(float("-inf")), "0")

    def test_duration_edge_cases(self):
        self.assertEqual(pa.duration(0), "0 sec")
        self.assertEqual(pa.duration(-50), "0 sec")
        self.assertEqual(pa.duration(59.9), "59 sec")
        self.assertEqual(pa.duration(60), "1 min")
        self.assertEqual(pa.duration(3599), "59 min")
        self.assertEqual(pa.duration(3600), "1h 0m")
        self.assertEqual(pa.duration(3665), "1h 1m")
        self.assertEqual(pa.duration(100_000_000), "27,777h 46m")

    def test_readme_rank_boundaries_and_extremes(self):
        self.assertEqual(pa.readme_rank(0, 0, 0, 0, 0, 0), "C")
        self.assertEqual(pa.readme_rank(-10, -5, -1, -2, -3, -4), "C")
        huge = 999_000_000_000_000
        self.assertEqual(pa.readme_rank(huge, huge, huge, huge, huge, huge), "S")
        seen = set()
        for scale in (0, 10, 50, 200, 600, 1500, 5000, 50000):
            seen.add(pa.readme_rank(scale, scale // 5, scale // 10, scale // 10, scale // 20, scale // 10))
        self.assertIn("C", seen)
        self.assertIn("S", seen)
        self.assertGreaterEqual(len(seen), 5)

    def test_save_escapes_xml_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            Path("images").mkdir()
            try:
                pa.save("test.svg", 'Rank <S> & "Quotes"', [], 120)
                root = _parse_svg(Path("images/test.svg"))
                title = root.find("{http://www.w3.org/2000/svg}title")
                self.assertEqual(title.text, 'Rank <S> & "Quotes"')
            finally:
                os.chdir(cwd)


class TestRankStreakRendering(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._cwd = os.getcwd()
        os.chdir(self._tmp.name)
        Path("images").mkdir()

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def test_all_nine_rank_themes_render_distinct_valid_svgs(self):
        today = date(2026, 4, 6)
        joined = date(2026, 4, 1)
        history = {joined + timedelta(days=i): 3 for i in range(6)}
        seen_gradients = set()
        for grade, theme in pa.RANK_THEMES.items():
            gradient, accent, badge, cur_col, long_col = theme
            pa.render_rank_streak(history, joined, today, grade)
            svg_text = Path("images/metrics-activity.svg").read_text(encoding="utf8")
            _parse_svg(Path("images/metrics-activity.svg"))
            self.assertIn(f"Grade {grade}", svg_text)
            self.assertIn(gradient[0], svg_text)
            self.assertIn(gradient[1], svg_text)
            self.assertIn(accent, svg_text)
            self.assertIn(f'fill="{badge}"', svg_text)
            self.assertIn(f'fill="{cur_col}"', svg_text)
            self.assertIn(f'fill="{long_col}"', svg_text)
            seen_gradients.add(gradient)
        self.assertEqual(len(seen_gradients), 9)

    def test_unknown_rank_falls_back_to_bronze(self):
        today = date(2026, 4, 6)
        pa.render_rank_streak({today: 1}, today, today, "SSS")
        svg_text = Path("images/metrics-activity.svg").read_text(encoding="utf8")
        _parse_svg(Path("images/metrics-activity.svg"))
        self.assertIn("#502820", svg_text)
        self.assertIn("#864413", svg_text)

    def test_streak_alive_when_yesterday_active_and_today_zero(self):
        today = date(2026, 4, 10)
        joined = date(2026, 4, 1)
        history = {date(2026, 4, 7): 1, date(2026, 4, 8): 2, date(2026, 4, 9): 3}
        pa.render_rank_streak(history, joined, today, "B-")
        svg_text = Path("images/metrics-activity.svg").read_text(encoding="utf8")
        self.assertIn("current streak 3 days, best 3 days", svg_text)

    def test_streak_broken_two_days_ago(self):
        today = date(2026, 4, 10)
        joined = date(2026, 4, 1)
        history = {date(2026, 4, 5): 2, date(2026, 4, 6): 2, date(2026, 4, 7): 2}
        pa.render_rank_streak(history, joined, today, "B")
        svg_text = Path("images/metrics-activity.svg").read_text(encoding="utf8")
        self.assertIn("current streak 0 days, best 3 days", svg_text)

    def test_streak_zero_contributions_and_joined_after_today(self):
        today = date(2026, 4, 10)
        pa.render_rank_streak({}, date(2026, 4, 12), today, "C")
        svg_text = Path("images/metrics-activity.svg").read_text(encoding="utf8")
        _parse_svg(Path("images/metrics-activity.svg"))
        self.assertIn("current streak 0 days, best 0 days", svg_text)
        self.assertNotIn("Best streak", svg_text)

    def test_singular_day_label_and_huge_contribution_count(self):
        today = date(2026, 4, 10)
        pa.render_rank_streak({today: 999_000_000_000_000}, today, today, "S")
        svg_text = Path("images/metrics-activity.svg").read_text(encoding="utf8")
        _parse_svg(Path("images/metrics-activity.svg"))
        self.assertIn(">1 day<", svg_text)
        self.assertIn("999,000,000,000,000 contributions", svg_text)


class TestWakaTimeAPIAndSecrets(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._cwd = os.getcwd()
        os.chdir(self._tmp.name)
        Path("images").mkdir()

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def test_waka_data_sends_bearer_token_and_extracts_data(self):
        captured = {}

        def fake_urlopen(req, timeout=0):
            captured["url"] = req.full_url
            captured["auth"] = req.get_header("Authorization")
            captured["timeout"] = timeout
            return _json_response({"data": {"total_seconds": 420}})

        with patch.object(pa, "urlopen", side_effect=fake_urlopen):
            res = pa.waka_data("all_time_since_today", "waka_secret_test_value")
        self.assertEqual(res, {"total_seconds": 420})
        self.assertEqual(captured["url"], "https://wakatime.com/api/v1/users/current/all_time_since_today")
        self.assertEqual(captured["auth"], "Bearer waka_secret_test_value")
        self.assertEqual(captured["timeout"], 15)

    def test_expired_wakatime_secret_never_leaks_and_renders_clean_svg(self):
        secret = "waka_super_secret_expired_token_999"
        err401 = HTTPError("https://wakatime.com/api/v1/...", 401, "Unauthorized", Message(), None)
        buf = io.StringIO()
        with patch.dict(os.environ, {"WAKATIME_TOKEN": secret}), \
             patch.object(pa, "urlopen", side_effect=err401), \
             contextlib.redirect_stdout(buf):
            pa.render_wakatime()

        svg_text = Path("images/metrics-wakatime.svg").read_text(encoding="utf8")
        _parse_svg(Path("images/metrics-wakatime.svg"))
        self.assertIn("WakaTime access expired or unauthorized.", svg_text)
        self.assertNotIn(secret, svg_text)
        self.assertNotIn(secret, buf.getvalue())
        self.assertIn("HTTP 401", buf.getvalue())
        self.assertIn("token may be expired", buf.getvalue())

    def test_waka_optional_handles_http_500_timeout_corrupt_json_and_null_data(self):
        secret = "waka_never_log_me"
        cases = [
            HTTPError("https://wakatime.com", 500, "Server Error", Message(), None),
            HTTPError("https://wakatime.com", 429, "Too Many Requests", Message(), None),
            URLError("dns failed"),
            TimeoutError("timed out"),
        ]
        for exc in cases:
            buf = io.StringIO()
            with patch.object(pa, "urlopen", side_effect=exc), contextlib.redirect_stdout(buf):
                self.assertEqual(pa.waka_optional("stats/all_time", secret, {}), {})
            self.assertNotIn(secret, buf.getvalue())

        # HTTP 200 with corrupt JSON
        with patch.object(pa, "urlopen", return_value=_FakeHTTPResponse(b"{broken json")), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pa.waka_optional("stats/all_time", secret, {}), {})

        # HTTP 200 with missing "data" key
        with patch.object(pa, "urlopen", return_value=_json_response({"error": "rate limit"})), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pa.waka_optional("stats/all_time", secret, {}), {})

        # HTTP 200 with {"data": null} -> rejected by type check, never crashes caller with NoneType
        with patch.object(pa, "urlopen", return_value=_json_response({"data": None})), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pa.waka_optional("summaries?start=a", secret, []), [])

    def test_render_wakatime_missing_and_whitespace_token(self):
        for val in (None, "", "   \n\t  "):
            with patch.dict(os.environ, {} if val is None else {"WAKATIME_TOKEN": val}, clear=False):
                if val is None:
                    os.environ.pop("WAKATIME_TOKEN", None)
                pa.render_wakatime()
            svg_text = Path("images/metrics-wakatime.svg").read_text(encoding="utf8")
            _parse_svg(Path("images/metrics-wakatime.svg"))
            self.assertIn("Waiting for WakaTime access.", svg_text)

    def test_render_wakatime_zero_activity_with_valid_token(self):
        responses = [_json_response({"data": {"total_seconds": 0}}),
                     _json_response({"data": {}}),
                     _json_response({"data": []})]
        with patch.dict(os.environ, {"WAKATIME_TOKEN": "tok"}), \
             patch.object(pa, "urlopen", side_effect=responses):
            pa.render_wakatime()
        svg_text = Path("images/metrics-wakatime.svg").read_text(encoding="utf8")
        _parse_svg(Path("images/metrics-wakatime.svg"))
        self.assertIn("Waiting for recorded coding activity.", svg_text)

    def test_render_wakatime_lagging_cache_falls_back_to_summaries(self):
        """When recent 7-day summaries exceed stale all_time cache, card shows 'or more tracked'."""
        days_payload = [
            {"range": {"date": "2026-11-26"}, "grand_total": {"total_seconds": 0}},
            {
                "range": {"date": "2026-11-27"},
                "grand_total": {
                    "total_seconds": 7200,
                    "ai_input_tokens": 100,
                    "ai_cached_input_tokens": 50,
                    "ai_output_tokens": 25,
                },
                "languages": [{"name": "Python", "total_seconds": 7200}],
            },
            # Corrupt entry inside summaries list should be safely skipped
            {"range": {"date": "2026-11-28"}, "grand_total": None},
        ]
        responses = [
            _json_response({"data": {"total_seconds": 3600}}),
            _json_response({"data": {"total_seconds": 3600}}),
            _json_response({"data": days_payload}),
        ]
        with patch.dict(os.environ, {"WAKATIME_TOKEN": "tok"}), \
             patch.object(pa, "urlopen", side_effect=responses):
            pa.render_wakatime()
        svg_text = Path("images/metrics-wakatime.svg").read_text(encoding="utf8")
        _parse_svg(Path("images/metrics-wakatime.svg"))
        self.assertIn("2h 0m or more tracked", svg_text)
        self.assertIn("27/11/2026", svg_text)
        self.assertIn("Python", svg_text)

    def test_render_wakatime_aggregate_with_recent_tokens_and_new_best_day(self):
        """Aggregate stats present; recent 7-day summary supplies cached tokens and beats best_day."""
        stats_payload = {
            "total_seconds_including_other_language": 36000,
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-11-27T10:00:00Z",
            "best_day": {"date": "2026-06-01", "text": "1h 0m", "total_seconds": 3600},
            "languages": [{"name": "Python", "percent": 100.0}],
            "ai_input_tokens": 1000,
            "ai_output_tokens": 200,
        }
        days_payload = [{
            "range": {"date": "2026-11-27"},
            "grand_total": {
                "total_seconds": 7200,
                "ai_input_tokens": 400,
                "ai_cached_input_tokens": 100,
                "ai_output_tokens": 50,
            },
        }]
        responses = [
            _json_response({"data": {"total_seconds": 36000}}),
            _json_response({"data": stats_payload}),
            _json_response({"data": days_payload}),
        ]
        with patch.dict(os.environ, {"WAKATIME_TOKEN": "tok"}), \
             patch.object(pa, "urlopen", side_effect=responses):
            pa.render_wakatime()
        svg_text = Path("images/metrics-wakatime.svg").read_text(encoding="utf8")
        _parse_svg(Path("images/metrics-wakatime.svg"))
        self.assertIn("10h 0m tracked since joining", svg_text)
        self.assertIn("Total tokens · 7 days", svg_text)
        self.assertIn("Output tokens · 7 days", svg_text)
        self.assertIn("27/11/2026", svg_text)
        self.assertIn("2h 0m", svg_text)

    def test_render_wakatime_aggregate_with_malformed_dates_and_no_best_day_overwrite(self):
        stats_payload = {
            "total_seconds": 18000,
            "start": "not-a-date",
            "end": "also-bad",
            "best_day": {"date": "2026-06-01", "text": "5h 0m", "total_seconds": 18000},
            "languages": [{"name": "Rust", "percent": 100.0}],
            "ai_cached_input_tokens": 50,
        }
        days_payload = [{
            "range": {"date": "2026-11-27"},
            "grand_total": {"total_seconds": 600},
        }]
        responses = [
            _json_response({"data": {"total_seconds": 18000}}),
            _json_response({"data": stats_payload}),
            _json_response({"data": days_payload}),
        ]
        with patch.dict(os.environ, {"WAKATIME_TOKEN": "tok"}), \
             patch.object(pa, "urlopen", side_effect=responses):
            pa.render_wakatime()
        svg_text = Path("images/metrics-wakatime.svg").read_text(encoding="utf8")
        _parse_svg(Path("images/metrics-wakatime.svg"))
        self.assertNotIn("Recorded breakdown", svg_text)
        self.assertIn("01/06/2026", svg_text)

    def test_render_wakatime_aggregate_without_start_end_keeps_rendering(self):
        stats_payload = {
            "total_seconds": 18000,
            "languages": [{"name": "Go", "percent": 100.0}],
        }
        responses = [
            _json_response({"data": {"total_seconds": 18000}}),
            _json_response({"data": stats_payload}),
            _json_response({"data": []}),
        ]
        with patch.dict(os.environ, {"WAKATIME_TOKEN": "tok"}), \
             patch.object(pa, "urlopen", side_effect=responses):
            pa.render_wakatime()
        svg_text = Path("images/metrics-wakatime.svg").read_text(encoding="utf8")
        _parse_svg(Path("images/metrics-wakatime.svg"))
        self.assertIn("Go", svg_text)


class TestSummaryStatsAndBreakdownEdgeCases(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._cwd = os.getcwd()
        os.chdir(self._tmp.name)
        Path("images").mkdir()

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def test_cached_tokens_variants(self):
        self.assertEqual(pa.cached_tokens({"ai_cached_input_tokens": 0}), 0)
        self.assertEqual(pa.cached_tokens({"ai_cached_input_tokens": 999_000_000_000_000}), 999_000_000_000_000)
        self.assertEqual(
            pa.cached_tokens({"ai_model_breakdown": [{"cached_input_tokens": 10}, {"cached_input_tokens": 25}]}),
            35,
        )
        self.assertIsNone(pa.cached_tokens({"ai_model_breakdown": [{"cached_input_tokens": 10}, {}]}))
        self.assertIsNone(pa.cached_tokens({}))

    def test_summary_stats_empty_zero_seconds_and_incomplete_cache(self):
        self.assertEqual(pa.summary_stats([]), {"ai_model_line_changes": {}})
        days = [
            {
                "range": {"date": "2026-11-25"},
                "grand_total": {
                    "total_seconds": 0,
                    "ai_input_tokens": 100,
                    "ai_cached_input_tokens": 20,
                },
                "languages": [{"name": "Python", "total_seconds": 0}],
            },
            {
                "range": {"date": "2026-11-26"},
                "grand_total": {
                    "total_seconds": 0,
                    "ai_input_tokens": 200,
                    # Missing cached tokens -> marks cache_complete = False
                },
            },
            {
                "range": {"date": "2026-11-27"},
                "grand_total": {"total_seconds": 0},
            },
        ]
        stats = pa.summary_stats(days)
        self.assertNotIn("ai_cached_input_tokens", stats)
        self.assertNotIn("languages", stats)
        self.assertNotIn("best_day", stats)
        self.assertEqual(stats["ai_input_tokens"], 300)

    def test_999_trillion_tokens_and_100_projects_render_cleanly(self):
        huge = 999_000_000_000_000
        days = [{
            "range": {"date": "2026-11-27"},
            "grand_total": {
                "total_seconds": 36000,
                "ai_additions": huge,
                "ai_deletions": huge,
                "human_additions": 500,
                "human_deletions": 200,
                "ai_sessions": huge,
                "ai_prompt_events_total": huge,
                "ai_input_tokens": huge,
                "ai_cached_input_tokens": huge,
                "ai_output_tokens": huge,
                "ai_model_total_cost": 50_000_000_000.0,
                "ai_model_line_changes": {f"model-{i:03d}": huge - i for i in range(100)},
            },
            "projects": [{"name": f"project-<special>&-{i:03d}-" + "x" * 30, "total_seconds": 1000 - i}
                         for i in range(100)],
            "languages": [{"name": f"Lang-{i:03d}", "total_seconds": 1000 - i} for i in range(100)],
            "editors": [{"name": f"Editor-{i:03d}", "total_seconds": 1000 - i} for i in range(100)],
            "categories": [{"name": f"Cat-{i:03d}", "total_seconds": 1000 - i} for i in range(100)],
            "operating_systems": [{"name": f"OS-{i:03d}", "total_seconds": 1000 - i} for i in range(100)],
        }]
        stats = pa.summary_stats(days)
        self.assertEqual(len(stats["projects"]), 100)
        body = pa.banner("WAKATIME  ·  ALL TIME", "10h 0m tracked")
        with patch.object(pa, "spend_vnd", return_value="1.3Q VND"):
            y = pa.waka_breakdown(body, 118, stats, period=(date(2026, 11, 20), date(2026, 11, 27)))
        pa.save("metrics-wakatime.svg", "WakaTime · stress", body, y + 20)

        svg_path = Path("images/metrics-wakatime.svg")
        root = _parse_svg(svg_path)
        svg_text = svg_path.read_text(encoding="utf8")
        self.assertIn("3.0Q", svg_text)
        self.assertIn("999.0T", svg_text)
        self.assertIn("1.3Q VND", svg_text)
        self.assertNotIn("999,000,000,000,000", svg_text)
        self.assertIn("project-&lt;special&gt;&amp;-004", svg_text)
        self.assertNotIn("project-&lt;special&gt;&amp;-005", svg_text)
        self.assertIn("model-004", svg_text)
        self.assertNotIn("model-005", svg_text)
        self.assertLess(int(root.attrib["height"]), 1800)

    def test_waka_breakdown_minimal_malformed_best_day_and_bar_clamping(self):
        body = []
        stats = {
            "best_day": {"date": "bad-date", "text": "1h"},
            "ai_prompt_events_total": 7,
            "ai_model_line_changes": {"zero-model": 0},
        }
        y = pa.waka_breakdown(body, 118, stats)
        self.assertGreater(y, 118)
        self.assertNotIn("Best day", "\n".join(body))

        # Unknown bar heading & percent clamping (-50 -> 0.0, 200 -> 145.0)
        bars_body = []
        pa.waka_bars(bars_body, 100, "Custom section", [("Neg", -50, "0%"), ("Over", 200, "200%")])
        joined_bars = "\n".join(bars_body)
        self.assertIn('fill="#8175d6"', joined_bars)
        self.assertIn('width="0.0"', joined_bars)
        self.assertIn('width="145.0"', joined_bars)

    def test_spend_vnd_zero_nonpositive_rate_fallback_and_unavailable(self):
        self.assertEqual(pa.spend_vnd(0), "0 VND")
        # First mirror returns rate <= 0, second mirror returns valid rate
        with patch.object(pa, "urlopen", side_effect=[
            _json_response({"usd": {"vnd": 0}}),
            _json_response({"usd": {"vnd": 25000}}),
        ]):
            self.assertEqual(pa.spend_vnd(2), "50,000 VND")

        with patch.object(pa, "urlopen", side_effect=[URLError("down"), _FakeHTTPResponse(b"not-json")]), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pa.spend_vnd(2), "Unavailable")


class TestGitHubQueriesAndMain(unittest.TestCase):
    def test_profile_rank_happy_path_filters_private_stars(self):
        user_payload = {
            "data": {
                "user": {
                    "createdAt": "2018-09-09T12:34:56Z",
                    "followers": {"totalCount": 50},
                    "pullRequests": {"totalCount": 20},
                    "issues": {"totalCount": 15},
                    "contributionsCollection": {"totalPullRequestReviewContributions": 10},
                }
            }
        }
        commits_payload = {"incomplete_results": False, "total_count": 500}
        repos_pages = [[
            {"stargazers_count": 30, "private": False},
            {"stargazers_count": 9999, "private": True},
        ]]
        with patch.object(pa.subprocess, "check_output", side_effect=[
            json.dumps(user_payload).encode(),
            json.dumps(commits_payload).encode(),
            json.dumps(repos_pages).encode(),
        ]):
            joined, rank = pa.profile_rank("wikiepeidia")
        self.assertEqual(joined, date(2018, 9, 9))
        self.assertIn(rank, pa.RANK_THEMES)
        # Private 9999 stars must not inflate rank to S
        self.assertNotEqual(rank, "S")

    def test_profile_rank_raises_on_incomplete_commit_search(self):
        user_payload = {
            "data": {
                "user": {
                    "createdAt": "2018-09-09T00:00:00Z",
                    "followers": {"totalCount": 10},
                    "pullRequests": {"totalCount": 5},
                    "issues": {"totalCount": 5},
                    "contributionsCollection": {"totalPullRequestReviewContributions": 2},
                }
            }
        }
        incomplete_commits = {"incomplete_results": True, "total_count": 100}
        with patch.object(pa.subprocess, "check_output", side_effect=[
            json.dumps(user_payload).encode(),
            json.dumps(incomplete_commits).encode(),
        ]):
            with self.assertRaises(RuntimeError):
                pa.profile_rank("wikiepeidia")

    def test_daily_history_multi_year_filtering_and_capping(self):
        commit_repos = [{"repository": {"id": f"r{i}", "visibility": "PUBLIC"}} for i in range(100)]
        year1 = {
            "data": {
                "user": {
                    "contributionsCollection": {
                        "commits": commit_repos,
                        "issues": [
                            {"repository": {"id": "priv1", "visibility": "PRIVATE"}},
                            {"repository": None},
                        ],
                        "prs": [],
                        "reviews": [],
                        "created": {"nodes": [], "pageInfo": {"hasNextPage": False}},
                        "contributionCalendar": {
                            "weeks": [{
                                "contributionDays": [
                                    # Out-of-range date before joined must be ignored
                                    {"date": "2025-12-30", "contributionCount": 99},
                                    {"date": "2025-12-31", "contributionCount": 4},
                                ]
                            }]
                        },
                    }
                }
            }
        }
        year2 = {
            "data": {
                "user": {
                    "contributionsCollection": {
                        "commits": [],
                        "issues": [],
                        "prs": [],
                        "reviews": [],
                        "created": {"nodes": [{"repository": {"id": "r100", "visibility": "PUBLIC"}}],
                                    "pageInfo": {"hasNextPage": False}},
                        "contributionCalendar": {
                            "weeks": [{
                                "contributionDays": [
                                    {"date": "2026-01-01", "contributionCount": 2},
                                    {"date": "2026-01-03", "contributionCount": 99},
                                ]
                            }]
                        },
                    }
                }
            }
        }
        with patch.object(pa.subprocess, "check_output", side_effect=[
            json.dumps(year1).encode(),
            json.dumps(year2).encode(),
        ]):
            history, repo_count = pa.daily_history("wikiepeidia", date(2025, 12, 31), date(2026, 1, 2))
        self.assertEqual(repo_count, "101+")
        self.assertEqual(history, {date(2025, 12, 31): 4, date(2026, 1, 1): 2})

    def test_main_writes_both_svgs_and_optional_github_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            Path("images").mkdir()
            gh_out = Path(tmp) / "gh_output.txt"
            try:
                # Run 1: with GITHUB_OUTPUT set
                with patch.dict(os.environ, {"GITHUB_REPOSITORY_OWNER": "wikiepeidia", "GITHUB_OUTPUT": str(gh_out)}, clear=False), \
                     patch.object(pa, "profile_rank", return_value=(date(2026, 1, 1), "A")), \
                     patch.object(pa, "daily_history", return_value=({date(2026, 1, 1): 5}, "12")):
                    os.environ.pop("WAKATIME_TOKEN", None)
                    pa.main()
                self.assertEqual(gh_out.read_text(encoding="utf8").strip(), "repo_count=12")
                _parse_svg(Path("images/metrics-activity.svg"))
                _parse_svg(Path("images/metrics-wakatime.svg"))

                # Run 2: without GITHUB_OUTPUT set
                with patch.dict(os.environ, {"GITHUB_REPOSITORY_OWNER": "wikiepeidia"}, clear=False), \
                     patch.object(pa, "profile_rank", return_value=(date(2026, 1, 1), "S")), \
                     patch.object(pa, "daily_history", return_value=({date(2026, 1, 1): 5}, "12")):
                    os.environ.pop("GITHUB_OUTPUT", None)
                    os.environ.pop("WAKATIME_TOKEN", None)
                    pa.main()
                _parse_svg(Path("images/metrics-activity.svg"))
            finally:
                os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
