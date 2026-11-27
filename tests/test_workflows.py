"""Unit tests validating GitHub Actions workflow files."""

import re
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"


def _load_workflow(name: str) -> dict:
    # PyYAML 1.1 parses unquoted `on:` as boolean True; normalize it.
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
                    # Verify every ${{ steps.<id>.outputs.<name> }} references an existing step id
                    blob = yaml.safe_dump(step)
                    for ref_id in re.findall(r"steps\.([a-zA-Z0-9_-]+)\.outputs\.", blob):
                        with self.subTest(workflow=wf_path.name, job=job_name, ref_id=ref_id):
                            self.assertIn(ref_id, step_ids, f"Unknown step id referenced: {ref_id}")

    def test_metrics_workflow_collects_all_generated_cards(self):
        raw = (WORKFLOWS_DIR / "metrics.yml").read_text(encoding="utf8")
        self.assertIn("metrics-overview.svg", raw)
        self.assertIn("metrics-{activity,wakatime}.svg", raw)


if __name__ == "__main__":
    unittest.main()
