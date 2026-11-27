"""Unit tests for .github/scripts/profile_activity.py."""

import io
import json
import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / ".github" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import profile_activity as pa


def _parse_svg(path: Path) -> ET.Element:
    """Assert the SVG is well-formed XML and return its root element."""
    content = path.read_text(encoding="utf8")
    return ET.fromstring(content)


class TestFormattingAndRank(unittest.TestCase):
    def test_clipped_boundaries(self):
        self.assertEqual(pa.clipped("", 24), "")
        self.assertEqual(pa.clipped("a" * 24, 24), "a" * 24)
        self.assertEqual(pa.clipped("a" * 25, 24), "a" * 23 + "…")

    def test_compact_number_normal_and_extreme(self):
        self.assertEqual(pa.compact_number(0), "0")
        self.assertEqual(pa.compact_number(999_999_999), "999,999,999")
        self.assertEqual(pa.compact_number(1_000_000_000), "1.0B")
        self.assertEqual(pa.compact_number(999_000_000_000_000), "999.0T")
        self.assertEqual(pa.compact_number(5_000_000_000_000_000), "5.0Q")
        self.assertEqual(pa.compact_number(2_500_000, threshold=1_000_000), "2.5M")
        self.assertEqual(pa.compact_number(999_999, threshold=1_000_000), "999,999")

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
        # Negative corrupted API values must not raise StopIteration
        self.assertEqual(pa.readme_rank(-10, -5, -1, -2, -3, -4), "C")
        # Huge 999 trillion counts saturate cleanly to S
        huge = 999_000_000_000_000
        self.assertEqual(pa.readme_rank(huge, huge, huge, huge, huge, huge), "S")
        # Intermediate progression reaches all expected tiers
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


class TestWakaTimeEdgeCases(unittest.TestCase):
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
        self.assertIsNone(
            pa.cached_tokens({"ai_model_breakdown": [{"cached_input_tokens": 10}, {}]})
        )
        self.assertIsNone(pa.cached_tokens({}))

    def test_999_trillion_tokens_and_100_projects_render_cleanly(self):
        """Stress test: 999T tokens, 999T line changes, huge VND spend, and 100 projects/languages."""
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
                "ai_model_total_cost": 50_000_000_000.0,  # $50B USD -> ~1.25Q VND
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
        self.assertEqual(len(stats["languages"]), 100)

        body = pa.banner("WAKATIME  ·  ALL TIME", "10h 0m tracked")
        with patch.object(pa, "spend_vnd", return_value="1.3Q VND"):
            y = pa.waka_breakdown(body, 118, stats, period=(date(2026, 11, 20), date(2026, 11, 27)))
        pa.save("metrics-wakatime.svg", "WakaTime · stress", body, y + 20)

        svg_path = Path("images/metrics-wakatime.svg")
        root = _parse_svg(svg_path)
        svg_text = svg_path.read_text(encoding="utf8")

        # 999T + 999T + 999T = 2.997Q -> compacted to 3.0Q, never raw 19-digit string
        self.assertIn("3.0Q", svg_text)
        self.assertIn("999.0T", svg_text)
        self.assertIn("1.3Q VND", svg_text)
        self.assertNotIn("999,000,000,000,000", svg_text)

        # Only top 5 of 100 projects / languages / models are rendered
        self.assertIn("project-&lt;special&gt;&amp;-000", svg_text)
        self.assertIn("project-&lt;special&gt;&amp;-004", svg_text)
        self.assertNotIn("project-&lt;special&gt;&amp;-005", svg_text)
        self.assertIn("Lang-004", svg_text)
        self.assertNotIn("Lang-005", svg_text)
        self.assertIn("model-004", svg_text)
        self.assertNotIn("model-005", svg_text)

        # Reasonable card height (7 sections * 5 rows, not 100 rows!)
        self.assertLess(int(root.attrib["height"]), 1800)

    def test_render_wakatime_without_token(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WAKATIME_TOKEN", None)
            pa.render_wakatime()
        svg_text = Path("images/metrics-wakatime.svg").read_text(encoding="utf8")
        _parse_svg(Path("images/metrics-wakatime.svg"))
        self.assertIn("Waiting for WakaTime access.", svg_text)

    def test_waka_optional_handles_network_failure(self):
        with patch.object(pa, "waka_data", side_effect=URLError("offline")):
            self.assertEqual(pa.waka_optional("stats/all_time", "tok", {"fallback": True}), {"fallback": True})

    def test_spend_vnd_fallback_and_unavailable(self):
        self.assertEqual(pa.spend_vnd(0), "0 VND")

        fake_resp = io.BytesIO(json.dumps({"usd": {"vnd": 25000}}).encode())
        fake_resp.__enter__ = lambda s: s
        fake_resp.__exit__ = lambda *a: None
        with patch.object(pa, "urlopen", side_effect=[URLError("cdn1 down"), fake_resp]):
            self.assertEqual(pa.spend_vnd(2), "50,000 VND")

        with patch.object(pa, "urlopen", side_effect=URLError("all down")):
            self.assertEqual(pa.spend_vnd(2), "Unavailable")


class TestGitHubQueries(unittest.TestCase):
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

    def test_daily_history_detects_capped_repos_and_filters_private(self):
        # 100 public commit repos triggers capped '+' suffix
        commit_repos = [{"repository": {"id": f"r{i}", "visibility": "PUBLIC"}} for i in range(100)]
        commit_repos.append({"repository": {"id": "priv1", "visibility": "PRIVATE"}})
        gql_response = {
            "data": {
                "user": {
                    "contributionsCollection": {
                        "commits": commit_repos[:100],
                        "issues": [{"repository": {"id": "priv1", "visibility": "PRIVATE"}}],
                        "prs": [],
                        "reviews": [],
                        "created": {"nodes": [], "pageInfo": {"hasNextPage": False}},
                        "contributionCalendar": {
                            "weeks": [{
                                "contributionDays": [
                                    {"date": "2026-01-01", "contributionCount": 4},
                                    {"date": "2026-01-02", "contributionCount": 0},
                                ]
                            }]
                        },
                    }
                }
            }
        }
        with patch.object(pa.subprocess, "check_output", return_value=json.dumps(gql_response).encode()):
            history, repo_count = pa.daily_history("wikiepeidia", date(2026, 1, 1), date(2026, 1, 2))
        self.assertEqual(repo_count, "100+")
        self.assertEqual(history, {date(2026, 1, 1): 4, date(2026, 1, 2): 0})


if __name__ == "__main__":
    unittest.main()
