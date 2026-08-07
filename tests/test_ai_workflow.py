import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ai_workflow", ROOT / "scripts/ai_workflow.py")
wf = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wf)


VALID_TASK = """# Goal
Implement a focused fixture change.
# Context
Integration fixture.
# Scope
## Allowed paths
- fixture.txt
## Forbidden paths
- secret/**
# Requirements
- Create the requested fixture.
# Forbidden changes
- No dependencies.
# Acceptance criteria
- The fixture exists.
# Validation commands
- true
# Decisions already made
- Use a text file.
# Open questions
- None.
"""
VALID_PLAN = {
    "version": 1, "status": "ready", "summary": "test plan",
    "steps": [{"id": "step-1", "type": "edit", "description": "edit fixture", "paths": ["fixture.txt"], "complexity": "mechanical", "preferred_executor": "executor"}],
    "validation": [], "escalation_conditions": [], "decisions_needed_from_chat": []
}


class UnitTests(unittest.TestCase):
    def test_config_and_precedence(self):
        config = wf.load_config(wf.config_path(ROOT))
        self.assertEqual(wf.profile_settings(config, None, {})["mode"], "normal")
        self.assertEqual(wf.profile_settings(config, "poor", {"AI_MODE": "normal"})["mode"], "poor")
        settings = wf.profile_settings(config, None, {"AI_MODE": "poor", "AI_PLANNER_MODEL": "custom"})
        self.assertEqual((settings["mode"], settings["planner_model"]), ("poor", "custom"))
        with self.assertRaises(wf.WorkflowError):
            wf.profile_settings(config, None, {"AI_MODE": "invalid"})

    def test_task_validation(self):
        with tempfile.TemporaryDirectory() as td:
            task = Path(td) / "TASK.md"
            task.write_text(VALID_TASK)
            wf.validate_task(task)
            task.write_text(VALID_TASK.replace("# Goal", "# Objective"))
            with self.assertRaises(wf.WorkflowError):
                wf.validate_task(task)
            task.write_text(VALID_TASK.replace("- The fixture exists.", ""))
            with self.assertRaises(wf.WorkflowError):
                wf.validate_task(task)
            task.write_text(VALID_TASK.replace("Implement a focused fixture change.", "Compare technologies and choose a framework architecture."))
            with self.assertRaises(wf.WorkflowError):
                wf.validate_task(task, "poor")

    def test_plan_validation(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "plan.json"
            valid = dict(VALID_PLAN)
            path.write_text(json.dumps(valid))
            wf.validate_plan(path)
            valid["status"] = "wrong"
            path.write_text(json.dumps(valid))
            with self.assertRaises(wf.WorkflowError):
                wf.validate_plan(path)
            path.write_text("not json")
            with self.assertRaises(wf.WorkflowError):
                wf.validate_plan(path)
            valid.update(status="needs_chat", steps=[], decisions_needed_from_chat=["Decide X"])
            path.write_text(json.dumps(valid))
            self.assertEqual(wf.validate_plan(path)["status"], "needs_chat")

    def test_router_command_profiles(self):
        config = wf.load_config(wf.config_path(ROOT))
        normal = wf.profile_settings(config, "normal", {})
        poor = wf.profile_settings(config, "poor", {})
        ncmd = wf.CodexAdapter(ROOT, normal, "codex").command("planner", "high", "phase", Path("out"), Path("schema"), True)
        pcmd = wf.CodexAdapter(ROOT, poor, "codex").command("executor", "low", "phase", Path("out"), Path("schema"))
        self.assertIn("--search", ncmd)
        self.assertNotIn("--search", pcmd)
        self.assertIn('model_reasoning_effort="high"', ncmd)
        self.assertIn('model_reasoning_effort="low"', pcmd)
        self.assertEqual(ncmd[ncmd.index("-m") + 1], "planner")

    def test_scope_and_dirty_snapshot(self):
        task = {"Allowed paths": "- src/**", "Forbidden paths": "- src/secret/**"}
        self.assertEqual(wf.scope_violations(["src/ok.py"], task), [])
        self.assertTrue(wf.scope_violations(["other.py", "src/secret/key"], task))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            dirty = root / "user.txt"
            dirty.write_text("user work")
            before = wf.file_snapshot(root)
            (root / "new.txt").write_text("new")
            self.assertEqual(dirty.read_text(), "user work")
            self.assertEqual(wf.changed_files(before, wf.file_snapshot(root)), ["new.txt"])


class IntegrationTests(unittest.TestCase):
    def fixture(self):
        td = tempfile.TemporaryDirectory()
        root = Path(td.name)
        shutil.copytree(ROOT / ".ai", root / ".ai", ignore=shutil.ignore_patterns("history"))
        shutil.copytree(ROOT / "scripts", root / "scripts")
        shutil.copy2(ROOT / "AGENTS.md", root / "AGENTS.md")
        (root / ".ai/TASK.md").write_text(VALID_TASK)
        (root / ".ai/RESULT.md").write_text("# Status\ncompleted\n")
        fake = ROOT / "tests/fake_codex.py"
        return td, root, fake

    def run_fixture(self, root, fake, extra=None, args=None):
        env = os.environ.copy()
        (root / "work").mkdir(exist_ok=True)
        env.update({"CODEX_BIN": str(fake), "FAKE_CAPTURE": str(root / "work/capture.jsonl"), "FAKE_STATE": str(root / "work/state")})
        env.update(extra or {})
        return subprocess.run([str(root / "scripts/ai-codex"), "run", "--poor", *(args or [])], cwd=root, env=env, text=True, capture_output=True)

    def runtime_paths(self, root):
        calls = [json.loads(line) for line in (root / "work/capture.jsonl").read_text().splitlines()]
        return [Path(call["args"][call["args"].index("-o") + 1]).parent for call in calls]

    def test_executor_model_fallback(self):
        td, root, fake = self.fixture()
        self.addCleanup(td.cleanup)
        proc = self.run_fixture(root, fake, {"FAKE_UNAVAILABLE_MODELS": "gpt-5.6-luna", "FAKE_EDIT_PATH": str(root / "fixture.txt")})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = (root / ".ai/RESULT.md").read_text()
        self.assertIn("Executor effective: gpt-5.6-terra", result)
        self.assertIn(str((root / ".ai/RESULT.md").resolve()), proc.stdout)
        self.assertFalse((root / ".ai/EXECUTION_PLAN.json").exists())
        runtime_paths = self.runtime_paths(root)
        self.assertTrue(runtime_paths)
        self.assertTrue(all(not str(path).startswith(str(root)) for path in runtime_paths))
        self.assertTrue(all(not path.exists() for path in runtime_paths))

    def test_escalation_returns_to_executor(self):
        td, root, fake = self.fixture()
        self.addCleanup(td.cleanup)
        proc = self.run_fixture(root, fake, {"FAKE_FAIL_EXECUTIONS": "1", "FAKE_EDIT_PATH": str(root / "fixture.txt")})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        phases = [json.loads(line)["phase"] for line in (root / "work/capture.jsonl").read_text().splitlines()]
        self.assertIn("ESCALATION_PHASE", phases)
        self.assertGreaterEqual(phases.count("EXECUTION_PHASE attempt 1"), 1)
        self.assertIn("Planner diagnosis", (root / ".ai/RESULT.md").read_text())

    def test_failed_run_prints_result_and_cleans_temp(self):
        td, root, fake = self.fixture()
        self.addCleanup(td.cleanup)
        proc = self.run_fixture(root, fake, {"FAKE_FAIL_EXECUTIONS": "99"})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(str((root / ".ai/RESULT.md").resolve()), proc.stdout)
        self.assertTrue(all(not path.exists() for path in self.runtime_paths(root)))

    def test_needs_chat_skips_executor(self):
        td, root, fake = self.fixture()
        self.addCleanup(td.cleanup)
        proc = self.run_fixture(root, fake, {"FAKE_NEEDS_CHAT": "1"})
        self.assertEqual(proc.returncode, 3)
        phases = [json.loads(line)["phase"] for line in (root / "work/capture.jsonl").read_text().splitlines()]
        self.assertEqual(phases, ["PLANNING_PHASE"])
        self.assertIn("Choose the persistence strategy", (root / ".ai/RESULT.md").read_text())
        self.assertIn(str((root / ".ai/RESULT.md").resolve()), proc.stdout)
        self.assertTrue(all(not path.exists() for path in self.runtime_paths(root)))

    def test_status_reports_absolute_result(self):
        td, root, fake = self.fixture()
        self.addCleanup(td.cleanup)
        proc = subprocess.run([str(root / "scripts/ai-codex"), "status"], cwd=root, text=True, capture_output=True)
        self.assertEqual(proc.returncode, 0)
        self.assertIn(str((root / ".ai/RESULT.md").resolve()), proc.stdout)
        self.assertIn("History:\n  disabled", proc.stdout)

    def test_history_disabled_then_bounded_by_run_count(self):
        td, root, fake = self.fixture()
        self.addCleanup(td.cleanup)
        proc = self.run_fixture(root, fake, {"FAKE_EDIT_PATH": str(root / "fixture.txt")})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse((root / ".ai/history").exists())
        config = wf.config_path(root)
        config.write_text(config.read_text().replace("max_runs: 5", "max_runs: 2"))
        for _ in range(4):
            proc = self.run_fixture(root, fake, {"FAKE_EDIT_PATH": str(root / "fixture.txt")}, ["--keep-history"])
            self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(wf.history_dirs(root)), 2)
        for entry in wf.history_dirs(root):
            self.assertEqual(sorted(path.name for path in entry.iterdir()), ["RESULT.md", "TASK.md"])

    def test_history_storage_limit_prunes(self):
        td, root, fake = self.fixture()
        self.addCleanup(td.cleanup)
        config = wf.config_path(root)
        config.write_text(config.read_text().replace("max_total_mb: 5", "max_total_mb: 1"))
        (root / ".ai/RESULT.md").write_text("x" * (2 * 1024 * 1024))
        proc = self.run_fixture(root, fake, {"FAKE_EDIT_PATH": str(root / "fixture.txt")}, ["--keep-history"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        total = sum(path.stat().st_size for entry in wf.history_dirs(root) for path in entry.rglob("*") if path.is_file())
        self.assertLessEqual(total, 1024 * 1024)

    def test_clean_dry_run_and_protected_files(self):
        td, root, fake = self.fixture()
        self.addCleanup(td.cleanup)
        (root / ".ai/EXECUTION_PLAN.json").write_text("{}")
        (root / ".ai/old.log").write_text("log")
        (root / ".ai/history/run").mkdir(parents=True)
        (root / ".ai/history/run/RESULT.md").write_text("old")
        protected = [root / ".ai/TASK.md", root / ".ai/RESULT.md", wf.config_path(root), root / "AGENTS.md", root / "scripts/ai_workflow.py"]
        before = {path: path.read_bytes() for path in protected}
        dry = subprocess.run([str(root / "scripts/ai-codex"), "clean", "--dry-run"], cwd=root, text=True, capture_output=True)
        self.assertEqual(dry.returncode, 0)
        self.assertTrue((root / ".ai/EXECUTION_PLAN.json").exists())
        clean = subprocess.run([str(root / "scripts/ai-codex"), "clean"], cwd=root, text=True, capture_output=True)
        self.assertEqual(clean.returncode, 0)
        self.assertFalse((root / ".ai/EXECUTION_PLAN.json").exists())
        self.assertFalse((root / ".ai/history").exists())
        self.assertEqual(before, {path: path.read_bytes() for path in protected})

    def test_result_and_log_excerpt_are_bounded(self):
        td, root, fake = self.fixture()
        self.addCleanup(td.cleanup)
        settings = wf.profile_settings(wf.load_config(wf.config_path(root)), "poor", {})
        settings.update(max_result_kb=1, max_log_excerpt_kb=1)
        wf.render_result(root, "failed", settings, "planner", "executor", "x" * 10000, failed=["y" * 10000])
        data = (root / ".ai/RESULT.md").read_bytes()
        self.assertLessEqual(len(data), 1024)
        self.assertNotIn(b"x" * 5000, data)
        self.assertIn(b"omitted", data)


if __name__ == "__main__":
    unittest.main()
