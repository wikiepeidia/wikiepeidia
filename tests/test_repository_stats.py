"""Comprehensive unit tests for .github/scripts/repository_stats.py."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / ".github" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import repository_stats as rs


class _FakeResponse:
    def __init__(self, payload, link="", raw_bytes=None):
        data = raw_bytes if raw_bytes is not None else json.dumps(payload).encode()
        self._buf = io.BytesIO(data)
        self.headers = {"Link": link} if link else {}

    def read(self, *args):
        return self._buf.read(*args)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


class TestContainerCount(unittest.TestCase):
    def test_pagination_and_org_repo_filtering(self):
        page1 = _FakeResponse(
            [{"repository": {"full_name": "alice/repo1"}}, {"repository": None}, "malformed-item"],
            link='<https://api.github.com/users/alice/packages?page=2>; rel="next", '
                 '<https://api.github.com/users/alice/packages?page=2>; rel="last"',
        )
        page2 = _FakeResponse([{"repository": {"full_name": "alice/repo2"}}])
        org_page = _FakeResponse([
            {"repository": {"full_name": "myorg/known"}},
            {"repository": {"full_name": "myorg/unrelated"}},
            {"repository": None},
        ])
        with patch.dict(os.environ, {"PACKAGES_TOKEN": "ghp_tok"}), \
             patch.object(rs, "urlopen", side_effect=[page1, page2, org_page]) as mock_open:
            total = rs.container_count(
                "alice",
                {"alice/repo1", "myorg/known"},
                {"alice": "User", "myorg": "Organization"},
            )
        # 4 from alice (owner counts all items) + 1 matching from myorg
        self.assertEqual(total, 5)
        self.assertEqual(mock_open.call_count, 3)

    def test_missing_or_whitespace_packages_token_degrades_gracefully(self):
        for val in (None, "", "   \n"):
            buf = io.StringIO()
            with patch.dict(os.environ, {} if val is None else {"PACKAGES_TOKEN": val}, clear=False), \
                 contextlib.redirect_stdout(buf):
                if val is None:
                    os.environ.pop("PACKAGES_TOKEN", None)
                self.assertIsNone(rs.container_count("alice", set(), {"alice": "User"}))
            self.assertIn("PACKAGES_TOKEN missing", buf.getvalue())

    def test_expired_and_rate_limited_tokens_never_leak_and_return_none(self):
        secret = "ghp_super_secret_packages_token_401"
        for code in (400, 401, 403, 404, 429):
            err = HTTPError("https://api.github.com", code, "Err", Message(), None)
            buf = io.StringIO()
            with patch.dict(os.environ, {"PACKAGES_TOKEN": secret}), \
                 patch.object(rs, "urlopen", side_effect=err), \
                 contextlib.redirect_stdout(buf):
                self.assertIsNone(rs.container_count("alice", set(), {"alice": "User"}))
            self.assertIn(f"HTTP {code}", buf.getvalue())
            self.assertNotIn(secret, buf.getvalue())

    def test_http_500_reraises(self):
        err500 = HTTPError("https://api.github.com", 500, "Server Error", Message(), None)
        with patch.dict(os.environ, {"PACKAGES_TOKEN": "tok"}), \
             patch.object(rs, "urlopen", side_effect=err500):
            with self.assertRaises(HTTPError):
                rs.container_count("alice", set(), {"alice": "User"})

    def test_network_timeout_and_corrupt_payloads_return_none(self):
        for exc in (URLError("dns error"), TimeoutError("timeout")):
            with patch.dict(os.environ, {"PACKAGES_TOKEN": "tok"}), \
                 patch.object(rs, "urlopen", side_effect=exc), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertIsNone(rs.container_count("alice", set(), {"alice": "User"}))

        # Corrupt JSON body
        with patch.dict(os.environ, {"PACKAGES_TOKEN": "tok"}), \
             patch.object(rs, "urlopen", return_value=_FakeResponse(None, raw_bytes=b"{bad")), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(rs.container_count("alice", set(), {"alice": "User"}))

        # Non-list JSON body (e.g. error dict on HTTP 200)
        with patch.dict(os.environ, {"PACKAGES_TOKEN": "tok"}), \
             patch.object(rs, "urlopen", return_value=_FakeResponse({"message": "unexpected"})), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(rs.container_count("alice", set(), {"alice": "User"}))

    def test_ssrf_cross_host_link_header_is_blocked_before_sending_token(self):
        evil_links = [
            '<https://evil.example/steal-token>; rel="next"',
            '<http://api.github.com/downgrade-http>; rel="next"',
        ]
        for link in evil_links:
            page1 = _FakeResponse([{"repository": {"full_name": "alice/r1"}}], link=link)
            buf = io.StringIO()
            with patch.dict(os.environ, {"PACKAGES_TOKEN": "ghp_do_not_exfiltrate"}), \
                 patch.object(rs, "urlopen", side_effect=[page1]) as mock_open, \
                 contextlib.redirect_stdout(buf):
                result = rs.container_count("alice", {"alice/r1"}, {"alice": "User"})
            self.assertIsNone(result)
            # Must NEVER make a second HTTP call with Authorization header to evil host
            self.assertEqual(mock_open.call_count, 1)
            self.assertIn("Refusing non-GitHub pagination URL", buf.getvalue())


class TestComputeStatsAndMain(unittest.TestCase):
    def _repo(self, name, owner="alice", kind="User", releases=0, packages=0, spdx=None):
        return {
            "nameWithOwner": f"{owner}/{name}",
            "owner": {"login": owner, "__typename": kind},
            "releases": {"totalCount": releases},
            "packages": {"totalCount": packages},
            "licenseInfo": {"spdxId": spdx} if spdx is not None else None,
        }

    def test_license_filtering_and_alphabetical_tie_break(self):
        pages = [{
            "data": {
                "user": {
                    "repositories": {
                        "nodes": [
                            self._repo("r1", spdx="MIT", releases=2, packages=1),
                            self._repo("r2", spdx="Apache-2.0", releases=3, packages=0),
                            self._repo("r3", spdx="NOASSERTION"),
                            self._repo("r4", spdx="NONE"),
                            self._repo("r5", spdx="Other"),
                            self._repo("r6", spdx=""),
                            self._repo("r7", spdx=None),
                        ]
                    }
                }
            }
        }]
        stats = rs.compute_stats("alice", pages, container_fn=lambda *a: 4)
        self.assertEqual(stats, {
            "releases": 5,
            "packages": 5,
            "containers_checked": True,
            "license": "Apache-2.0",
        })

    def test_empty_repos_and_unavailable_containers(self):
        pages = [{"data": {"user": {"repositories": {"nodes": []}}}}]
        stats = rs.compute_stats("alice", pages, container_fn=lambda *a: None)
        self.assertEqual(stats, {
            "releases": 0,
            "packages": 0,
            "containers_checked": False,
            "license": None,
        })

    def test_150_repos_across_two_graphql_pages(self):
        page1 = {"data": {"user": {"repositories": {"nodes": [
            self._repo(f"r{i}", releases=1, packages=2, spdx="MIT") for i in range(100)
        ]}}}}
        page2 = {"data": {"user": {"repositories": {"nodes": [
            self._repo(f"r{i}", owner="org1", kind="Organization", releases=1, spdx="GPL-3.0")
            for i in range(100, 150)
        ]}}}}
        stats = rs.compute_stats("alice", [page1, page2], container_fn=lambda *a: 10)
        self.assertEqual(stats["releases"], 150)
        self.assertEqual(stats["packages"], 210)
        self.assertEqual(stats["license"], "MIT")

    def test_main_writes_stats_json_to_github_output(self):
        pages = [{"data": {"user": {"repositories": {"nodes": [
            self._repo("demo", owner="wikiepeidia", releases=7, packages=3, spdx="MIT")
        ]}}}}]
        with tempfile.TemporaryDirectory() as tmp:
            gh_out = Path(tmp) / "gh_out.txt"
            with patch.dict(os.environ, {
                "GITHUB_REPOSITORY_OWNER": "wikiepeidia",
                "GITHUB_OUTPUT": str(gh_out),
                "PACKAGES_TOKEN": "tok",
            }), \
                 patch.object(rs.subprocess, "check_output", return_value=json.dumps(pages).encode()), \
                 patch.object(rs, "urlopen", return_value=_FakeResponse([{"repository": {"full_name": "wikiepeidia/demo"}}])):
                rs.main()
            line = gh_out.read_text(encoding="utf8").strip()
        self.assertTrue(line.startswith("stats="))
        parsed = json.loads(line.removeprefix("stats="))
        self.assertEqual(parsed, {
            "releases": 7,
            "packages": 4,
            "containers_checked": True,
            "license": "MIT",
        })


if __name__ == "__main__":
    unittest.main()
