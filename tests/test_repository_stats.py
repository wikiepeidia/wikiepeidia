"""Unit tests for .github/scripts/repository_stats.py."""

import io
import json
import os
import sys
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / ".github" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import repository_stats as rs


class _FakeResponse:
    def __init__(self, payload, link=""):
        self._buf = io.BytesIO(json.dumps(payload).encode())
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
            [{"repository": {"full_name": "alice/repo1"}}, {"repository": None}],
            link='<https://api.github.com/users/alice/packages?page=2>; rel="next", '
                 '<https://api.github.com/users/alice/packages?page=2>; rel="last"',
        )
        page2 = _FakeResponse([{"repository": {"full_name": "alice/repo2"}}])
        org_page = _FakeResponse([
            {"repository": {"full_name": "myorg/known"}},
            {"repository": {"full_name": "myorg/unrelated"}},
            {"repository": None},
        ])
        with patch.dict(os.environ, {"PACKAGES_TOKEN": "tok"}), \
             patch.object(rs, "urlopen", side_effect=[page1, page2, org_page]) as mock_open:
            total = rs.container_count(
                "alice",
                {"alice/repo1", "myorg/known"},
                {"alice": "User", "myorg": "Organization"},
            )
        # 3 from alice (owner counts all) + 1 matching from myorg
        self.assertEqual(total, 4)
        self.assertEqual(mock_open.call_count, 3)

    def test_graceful_none_on_403_and_raises_on_500(self):
        err403 = HTTPError("https://api.github.com", 403, "Forbidden", Message(), None)
        with patch.dict(os.environ, {"PACKAGES_TOKEN": "tok"}), \
             patch.object(rs, "urlopen", side_effect=err403):
            self.assertIsNone(rs.container_count("alice", set(), {"alice": "User"}))

        err500 = HTTPError("https://api.github.com", 500, "Server Error", Message(), None)
        with patch.dict(os.environ, {"PACKAGES_TOKEN": "tok"}), \
             patch.object(rs, "urlopen", side_effect=err500):
            with self.assertRaises(HTTPError):
                rs.container_count("alice", set(), {"alice": "User"})


class TestComputeStats(unittest.TestCase):
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
            "license": "Apache-2.0",  # tie (1 vs 1) broken alphabetically
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


if __name__ == "__main__":
    unittest.main()
