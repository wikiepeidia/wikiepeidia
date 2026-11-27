"""Unit tests validating GitHub Actions workflows, README integrity, extras_js, and security."""

import json
import re
import shutil
import subprocess
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
README_PATH = REPO_ROOT / "README.md"


def _load_workflow(name: str) -> dict:
    data = yaml.safe_load((WORKFLOWS_DIR / name).read_text(encoding="utf8"))
    if True in data and "on" not in data:
        data["on"] = data.pop(True)
    return data


class TestWorkflows(unittest.TestCase):
    def test_all_workflows_are_valid_yaml_with_required_keys(self):
        files = list(WORKFLOWS_DIR.glob("*.yml")) + list(WORKFLOWS_DIR.glob("*.yaml"))
        self.assertGreaterEqual(len(files), 1, "Expected at least one workflow file")
        for wf_path in files:
            with self.subTest(workflow=wf_path.name):
                data = _load_workflow(wf_path.name)
                self.assertIn("name", data)
                self.assertIn("on", data)
                self.assertIn("jobs", data)
                self.assertTrue(data["jobs"], "Workflow must define at least one job")

    def test_cron_schedules_have_five_fields(self):
        for wf_path in WORKFLOWS_DIR.glob("*.yml"):
            data = _load_workflow(wf_path.name)
            schedules = (data.get("on") or {}).get("schedule") or []
            for entry in schedules:
                cron = entry["cron"]
                with self.subTest(workflow=wf_path.name, cron=cron):
                    self.assertEqual(len(cron.split()), 5, f"Invalid cron expression: {cron}")

    def test_push_paths_exist_in_repo(self):
        for wf_path in WORKFLOWS_DIR.glob("*.yml"):
            data = _load_workflow(wf_path.name)
            push = (data.get("on") or {}).get("push") or {}
            for rel_path in push.get("paths") or []:
                if any(ch in rel_path for ch in "*?[]"):
                    continue
                with self.subTest(workflow=wf_path.name, path=rel_path):
                    self.assertTrue((REPO_ROOT / rel_path).exists(), f"Missing trigger path: {rel_path}")

    def test_referenced_python_scripts_and_step_outputs_exist(self):
        for wf_path in WORKFLOWS_DIR.glob("*.yml"):
            data = _load_workflow(wf_path.name)
            for job_name, job in (data.get("jobs") or {}).items():
                steps = job.get("steps") or []
                step_ids = {step["id"] for step in steps if "id" in step}
                for step in steps:
                    run_cmd = step.get("run") or ""
                    for script in re.findall(r"python3?\s+([^\s]+\.py)", run_cmd):
                        with self.subTest(workflow=wf_path.name, job=job_name, script=script):
                            self.assertTrue((REPO_ROOT / script).exists(), f"Missing script: {script}")
                    blob = yaml.safe_dump(step)
                    for ref_id in re.findall(r"steps\.([a-zA-Z0-9_-]+)\.outputs\.", blob):
                        with self.subTest(workflow=wf_path.name, job=job_name, ref_id=ref_id):
                            self.assertIn(ref_id, step_ids, f"Unknown step id referenced: {ref_id}")

    def test_metrics_workflow_collects_all_generated_cards_and_pins_image_digest(self):
        raw = (WORKFLOWS_DIR / "metrics.yml").read_text(encoding="utf8")
        self.assertIn("metrics-overview.svg", raw)
        self.assertIn("metrics-{activity,wakatime}.svg", raw)
        # Supply-chain check: container image must be pinned by immutable sha256 digest
        self.assertRegex(raw, r"ghcr\.io/lowlighter/metrics@sha256:[0-9a-f]{64}")

    @unittest.skipUnless(shutil.which("node"), "Node.js required to execute workflow extras_js")
    def test_metrics_workflow_extras_js_transforms_fields_correctly(self):
        wf = _load_workflow("metrics.yml")
        steps = wf["jobs"]["render"]["steps"]
        metrics_step = next(s for s in steps if str(s.get("uses", "")).startswith("lowlighter/metrics"))
        template_js = metrics_step["with"]["extras_js"]

        def run_extras(stats_obj: dict, repo_count: str, fields: list[str]) -> list[str]:
            js_code = template_js.replace("${{ steps.repositories.outputs.stats }}", json.dumps(stats_obj))
            js_code = js_code.replace("${{ steps.activity.outputs.repo_count }}", repo_count)
            harness = f"""
const fields = {json.dumps(fields)}.map(text => ({{
  textContent: text,
  svg: {{ tag: "svg" }},
  querySelector() {{ return this.svg; }},
  replaceChildren(_svg, node) {{ this.textContent = node.data.trim(); }},
}}));
const document = {{
  querySelectorAll() {{ return fields; }},
  createTextNode(s) {{ return {{ data: s }}; }},
}};
{js_code}
console.log(JSON.stringify(fields.map(f => f.textContent)));
"""
            out = subprocess.check_output(["node", "-e", harness], text=True)
            return json.loads(out)

        # Normal case with containers checked (PACKAGES_TOKEN valid)
        out_ok = run_extras(
            {"releases": 1, "packages": 12, "containers_checked": True, "license": "MIT"},
            "42+",
            [
                "Contributed to 19 repositories",
                "Prefers Apache-2.0 license",
                "3 releases",
                "0 packages",
                "Unrelated field",
            ],
        )
        self.assertEqual(out_ok, [
            "Contributed to 42+ public repositories",
            "Top license: MIT",
            "1 Release",
            "12 public packages",
            "Unrelated field",
        ])

        # Fallback case: expired PACKAGES_TOKEN (containers_checked=False), no license, 1 package, huge releases
        out_degraded = run_extras(
            {"releases": 999999, "packages": 1, "containers_checked": False, "license": None},
            "1",
            [
                "Contributed to 1 repository",
                "No license preference",
                "1 release",
                "1 package",
            ],
        )
        self.assertEqual(out_degraded, [
            "Contributed to 1 public repositories",
            "No recognized license",
            "999999 Releases",
            "1 registry package",
        ])


class TestReadmeIntegrityAndSecurity(unittest.TestCase):
    def test_readme_local_assets_exist_and_svgs_are_valid_xml(self):
        readme = README_PATH.read_text(encoding="utf8")
        local_paths = set(re.findall(r'(?:src|href)="(images/[^"]+)"', readme))
        self.assertGreaterEqual(len(local_paths), 1, "Expected local assets referenced in README.md")
        for rel in sorted(local_paths):
            asset = REPO_ROOT / rel
            with self.subTest(asset=rel):
                self.assertTrue(asset.exists(), f"README references missing local asset: {rel}")
                if asset.suffix.lower() == ".svg":
                    ET.fromstring(asset.read_text(encoding="utf8"))

    def test_readme_generated_card_urls_match_workflow_branches(self):
        readme = README_PATH.read_text(encoding="utf8")
        metrics_wf = _load_workflow("metrics.yml")
        snake_wf = _load_workflow("snake.yml")

        publish_branches = set()
        for wf in (metrics_wf, snake_wf):
            for job in wf["jobs"].values():
                for step in job.get("steps") or []:
                    target = (step.get("with") or {}).get("target_branch")
                    if target:
                        publish_branches.add(target)

        # Every raw.githubusercontent.com/wikiepeidia/wikiepeidia/<branch>/... URL must target a published branch
        card_urls = re.findall(
            r"https://raw\.githubusercontent\.com/wikiepeidia/wikiepeidia/([^/]+)/([^\s\"']+)",
            readme,
        )
        self.assertGreaterEqual(len(card_urls), 5)
        for branch, rel_file in card_urls:
            with self.subTest(branch=branch, file=rel_file):
                self.assertIn(branch, publish_branches, f"README references unpublished branch: {branch}")

        # Verify all three metrics-output SVGs exist in metrics.yml
        metrics_raw = (WORKFLOWS_DIR / "metrics.yml").read_text(encoding="utf8")
        self.assertIn("images/metrics-overview.svg", readme)
        self.assertIn("images/metrics-activity.svg", readme)
        self.assertIn("images/metrics-wakatime.svg", readme)
        self.assertIn("metrics-overview.svg", metrics_raw)

    def test_no_hardcoded_secrets_in_tracked_files(self):
        secret_patterns = [
            re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
            re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
            re.compile(r"waka_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"),
            re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        ]
        files_to_scan = [
            README_PATH,
            *(REPO_ROOT / ".github" / "scripts").glob("*.py"),
            *WORKFLOWS_DIR.glob("*.yml"),
        ]
        for path in files_to_scan:
            text = path.read_text(encoding="utf8")
            for pattern in secret_patterns:
                with self.subTest(file=str(path.relative_to(REPO_ROOT)), pattern=pattern.pattern):
                    self.assertIsNone(pattern.search(text), f"Potential hardcoded secret in {path}")


if __name__ == "__main__":
    unittest.main()
