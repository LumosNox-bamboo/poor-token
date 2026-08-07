#!/usr/bin/env python3
"""Small, dependency-free quota-aware Codex orchestrator."""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional, Sequence, Tuple

SAFE_DEFAULTS = {
    "default_mode": "normal",
    "planner": "gpt-5.6-sol",
    "planner_fallbacks": ["gpt-5.6"],
    "executor": "gpt-5.6-luna",
    "executor_fallbacks": ["gpt-5.6-terra"],
}
REQUIRED_TASK = ("Goal", "Scope", "Requirements", "Acceptance criteria")
CONTROLLED_ARTIFACTS = {".ai/RESULT.md"}
PLAN_SCHEMA = {
    "type": "object",
    "required": ["version", "status", "summary", "steps", "validation", "escalation_conditions", "decisions_needed_from_chat"],
    "properties": {"version": {"const": 1}, "status": {"enum": ["ready", "needs_chat"]},
                   "summary": {"type": "string"}, "steps": {"type": "array"}, "validation": {"type": "array"},
                   "escalation_conditions": {"type": "array"}, "decisions_needed_from_chat": {"type": "array"}},
}
EXECUTION_SCHEMA = {
    "type": "object",
    "required": ["status", "summary", "passed", "failed", "decisions_needed_from_chat"],
    "properties": {"status": {"enum": ["completed", "failed", "needs_chat"]}, "summary": {"type": "string"},
                   "error": {"type": "string"}, "passed": {"type": "array", "items": {"type": "string"}},
                   "failed": {"type": "array", "items": {"type": "string"}},
                   "decisions_needed_from_chat": {"type": "array", "items": {"type": "string"}}},
}


class WorkflowError(Exception):
    def __init__(self, message: str, kind: str = "failed_execution") -> None:
        super().__init__(message)
        self.kind = kind


def project_root(start: Optional[Path] = None) -> Path:
    here = (start or Path(__file__).resolve().parent).resolve()
    if here.is_file():
        here = here.parent
    for candidate in (here, *here.parents):
        if config_path(candidate).exists():
            return candidate
    raise WorkflowError("Cannot find project root containing .ai/config.yaml or .ai/config.example.yaml", "configuration_error")


def config_path(root: Path) -> Path:
    local = root / ".ai/config.yaml"
    return local if local.exists() else root / ".ai/config.example.yaml"


def scalar(value: str) -> Any:
    value = value.strip()
    if value in ("true", "false"):
        return value == "true"
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value.strip("\"'")


def load_config(path: Path) -> Dict[str, Any]:
    """Parse the deliberately small YAML subset used by this project."""
    root: Dict[str, Any] = {}
    stack: List[Tuple[int, Any]] = [(-1, root)]
    last_key: Dict[int, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        text = raw.strip()
        while stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        if text.startswith("- "):
            if not isinstance(parent, list):
                raise WorkflowError(f"Unsupported config list at line {number}", "configuration_error")
            parent.append(scalar(text[2:]))
            continue
        if ":" not in text or not isinstance(parent, dict):
            raise WorkflowError(f"Malformed config at line {number}", "configuration_error")
        key, value = text.split(":", 1)
        if value.strip():
            parent[key] = scalar(value)
        else:
            # Look ahead is avoided: known plural keys are lists; other nodes are maps.
            node: Any = [] if key in {"fallbacks", "delegate_to_chat"} else {}
            parent[key] = node
            stack.append((indent, node))
            last_key[indent] = key
    if root.get("version") != 1:
        raise WorkflowError(".ai/config.yaml must have version: 1", "configuration_error")
    return root


def profile_settings(config: Dict[str, Any], cli_mode: Optional[str], env: Dict[str, str]) -> Dict[str, Any]:
    mode = cli_mode or env.get("AI_MODE") or config.get("default_mode") or SAFE_DEFAULTS["default_mode"]
    if mode not in ("normal", "poor"):
        raise WorkflowError(f"Invalid mode {mode!r}; expected normal or poor", "configuration_error")
    profiles = config.get("profiles", {})
    profile = profiles.get(mode, {})
    models = config.get("models", {})
    planner = models.get("planner", {})
    executor = models.get("executor", {})
    storage = config.get("storage", {})
    history = config.get("history", {})
    return {
        "mode": mode,
        "planner_model": env.get("AI_PLANNER_MODEL") or planner.get("preferred") or SAFE_DEFAULTS["planner"],
        "planner_fallbacks": planner.get("fallbacks") or SAFE_DEFAULTS["planner_fallbacks"],
        "executor_model": env.get("AI_EXECUTOR_MODEL") or executor.get("preferred") or SAFE_DEFAULTS["executor"],
        "executor_fallbacks": executor.get("fallbacks") or SAFE_DEFAULTS["executor_fallbacks"],
        "planner_reasoning": profile.get("planner", {}).get("reasoning", "medium"),
        "executor_reasoning": profile.get("executor", {}).get("reasoning", "low"),
        "network": bool(profile.get("network", mode == "normal")),
        "browser": bool(profile.get("browser", mode == "normal")),
        "external_research": bool(profile.get("external_research", mode == "normal")),
        "max_repairs": int(profile.get("max_executor_repair_attempts", 2)),
        "escalate_after": int(profile.get("escalate_after_failed_repairs", 1)),
        "max_result_kb": max(1, int(storage.get("max_result_kb", 128))),
        "max_log_excerpt_kb": max(1, int(storage.get("max_log_excerpt_kb", 32))),
        "temp_cleanup": bool(storage.get("temp_cleanup", True)),
        "history_enabled": bool(history.get("enabled", False)),
        "history_max_runs": max(1, int(history.get("max_runs", 5))),
        "history_max_total_mb": max(1, int(history.get("max_total_mb", 5))),
    }


def parse_task(path: Path) -> Dict[str, str]:
    if not path.exists():
        raise WorkflowError(f"Task file not found: {path}", "failed_validation")
    sections: Dict[str, List[str]] = {}
    current: Optional[str] = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^(#{1,2})\s+(.+?)\s*$", raw)
        if match:
            current = match.group(2)
            sections.setdefault(current, [])
        elif current is not None:
            sections[current].append(raw)
    return {key: "\n".join(value).strip() for key, value in sections.items()}


def path_items(text: str) -> List[str]:
    return [m.group(1).strip().strip("`") for line in text.splitlines() if (m := re.match(r"^\s*[-*]\s+(.+?)\s*$", line))]


def validate_task(path: Path, mode: str = "normal") -> Dict[str, str]:
    sections = parse_task(path)
    errors = []
    for section in REQUIRED_TASK:
        if section not in sections:
            errors.append(f"Missing required section: # {section}")
        elif section == "Scope" and (sections.get("Allowed paths") or sections.get("Forbidden paths")):
            continue
        elif not sections[section] or sections[section].lower() in {"todo", "tbd", "n/a"}:
            errors.append(f"Section must not be empty or placeholder: # {section}")
    if len(path.read_text(encoding="utf-8").strip()) < 80:
        errors.append("Task is too short to be usable")
    allowed = path_items(sections.get("Allowed paths", ""))
    forbidden = path_items(sections.get("Forbidden paths", ""))
    exact = set(allowed) & set(forbidden)
    if exact:
        errors.append("Paths are both allowed and forbidden: " + ", ".join(sorted(exact)))
    if mode == "poor":
        body = " ".join(sections.values()).lower()
        concepts = [
            (r"(compare|research|evaluate).{0,35}(framework|technolog|architecture|best practice)", "broad research or technology selection"),
            (r"(choose|select|decide).{0,30}(framework|architecture|infrastructure|database)", "an architecture decision"),
            (r"(product strategy|business requirement|unspecified requirements)", "a product or requirements decision"),
        ]
        for pattern, label in concepts:
            if re.search(pattern, body):
                errors.append(
                    f"This task appears to require {label}, which is excluded from poor mode. "
                    "Move the reasoning step to ChatGPT, record the decision under "
                    "'Decisions already made', then run Codex again."
                )
                break
    if errors:
        raise WorkflowError("\n".join(errors), "failed_validation")
    return sections


def validate_plan(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"Malformed plan JSON: {exc}", "failed_planning")
    required = {"version", "status", "summary", "steps", "validation", "escalation_conditions", "decisions_needed_from_chat"}
    missing = sorted(required - data.keys()) if isinstance(data, dict) else sorted(required)
    if missing:
        raise WorkflowError("Plan missing fields: " + ", ".join(missing), "failed_planning")
    if data["version"] != 1 or data["status"] not in ("ready", "needs_chat"):
        raise WorkflowError("Plan version/status is invalid", "failed_planning")
    if not isinstance(data["summary"], str) or not data["summary"].strip():
        raise WorkflowError("Plan summary must be non-empty", "failed_planning")
    if not isinstance(data["steps"], list) or (data["status"] == "ready" and not data["steps"]):
        raise WorkflowError("A ready plan must contain steps", "failed_planning")
    if data["status"] == "needs_chat" and not data["decisions_needed_from_chat"]:
        raise WorkflowError("needs_chat plan must name decisions", "failed_planning")
    for step in data["steps"]:
        fields = {"id", "type", "description", "paths", "complexity", "preferred_executor"}
        if not isinstance(step, dict) or fields - step.keys() or step.get("complexity") not in ("mechanical", "routine", "reasoning"):
            raise WorkflowError("Plan contains an invalid step", "failed_planning")
    return data


def file_snapshot(root: Path) -> Dict[str, str]:
    result = {}
    for path in root.rglob("*"):
        rel = path.relative_to(root).as_posix()
        if not path.is_file() or rel.startswith((".git/", ".ai/history/", "work/", "outputs/")) or rel in CONTROLLED_ARTIFACTS:
            continue
        try:
            result[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            pass
    return result


def changed_files(before: Dict[str, str], after: Dict[str, str]) -> List[str]:
    return sorted(key for key in before.keys() | after.keys() if before.get(key) != after.get(key))


def matches(path: str, pattern: str) -> bool:
    pattern = pattern.lstrip("./")
    if pattern.endswith("/**"):
        return path == pattern[:-3] or path.startswith(pattern[:-2])
    return fnmatch.fnmatchcase(path, pattern)


def scope_violations(changed: Sequence[str], task: Dict[str, str]) -> List[str]:
    allowed = path_items(task.get("Allowed paths", ""))
    forbidden = path_items(task.get("Forbidden paths", ""))
    violations = []
    for path in changed:
        if any(matches(path, item) for item in forbidden):
            violations.append(f"{path} (forbidden)")
        elif allowed and not any(matches(path, item) for item in allowed):
            violations.append(f"{path} (outside allowed paths)")
    return violations


class CodexAdapter:
    def __init__(self, root: Path, settings: Dict[str, Any], executable: str) -> None:
        self.root, self.settings, self.executable = root, settings, executable

    def command(self, model: str, reasoning: str, phase: str, output: Path, schema: Path, readonly: bool = False) -> List[str]:
        cmd = [self.executable]
        if self.settings["mode"] == "normal" and self.settings["network"]:
            # --search is a top-level option in codex-cli 0.145.0, not an exec option.
            cmd.append("--search")
        cmd.extend(["exec", "--ephemeral", "--skip-git-repo-check", "-C", str(self.root), "-m", model,
               "-c", f'model_reasoning_effort="{reasoning}"', "-s", "read-only" if readonly else "workspace-write",
               "-a", "never", "--output-schema", str(schema), "-o", str(output)])
        cmd.append(phase)
        return cmd

    def invoke(self, models: Sequence[str], reasoning: str, phase: str, output: Path, schema: Path, readonly: bool = False) -> Tuple[str, subprocess.CompletedProcess[str]]:
        failures = []
        for model in models:
            try:
                proc = subprocess.run(self.command(model, reasoning, phase, output, schema, readonly), cwd=self.root, text=True, capture_output=True)
            except OSError as exc:
                raise WorkflowError(f"Codex executable is unavailable: {exc}", "codex_unavailable")
            if proc.returncode == 0 and output.exists():
                return model, proc
            failures.append(f"{model}: {(proc.stderr or proc.stdout).strip()[-800:]}")
            unavailable = re.search(r"model.{0,40}(not found|unavailable|unsupported|access)", (proc.stderr + proc.stdout), re.I)
            if not unavailable:
                break
        raise WorkflowError("Codex invocation failed: " + " | ".join(failures), "failed_execution")


def write_runtime_schemas(runtime: Path) -> Tuple[Path, Path]:
    plan_schema = runtime / "execution-plan.schema.json"
    execution_schema = runtime / "execution-result.schema.json"
    plan_schema.write_text(json.dumps(PLAN_SCHEMA), encoding="utf-8")
    execution_schema.write_text(json.dumps(EXECUTION_SCHEMA), encoding="utf-8")
    return plan_schema, execution_schema


def history_dirs(root: Path) -> List[Path]:
    history = root / ".ai/history"
    return sorted((path for path in history.iterdir() if path.is_dir()), key=lambda path: path.name) if history.exists() else []


def prune_history(root: Path, settings: Dict[str, Any]) -> None:
    entries = history_dirs(root)
    byte_limit = settings["history_max_total_mb"] * 1024 * 1024
    def total_size() -> int:
        return sum(path.stat().st_size for entry in entries for path in entry.rglob("*") if path.is_file())
    while entries and (len(entries) > settings["history_max_runs"] or total_size() > byte_limit):
        shutil.rmtree(entries.pop(0))


def archive_artifacts(root: Path, settings: Dict[str, Any], enabled: bool) -> None:
    if not enabled:
        return
    existing = [root / ".ai/TASK.md", root / ".ai/RESULT.md"]
    if not any(path.exists() for path in existing):
        return
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H%M%S.%fZ")
    target = root / ".ai/history" / stamp
    target.mkdir(parents=True, exist_ok=False)
    for path in existing:
        if path.exists():
            shutil.copy2(path, target / path.name)
    prune_history(root, settings)


def bounded_text(value: Any, max_bytes: int) -> str:
    text = str(value)
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    return raw[:max_bytes].decode("utf-8", errors="ignore") + "\n[verbose output omitted]"


def render_result(root: Path, status: str, settings: Dict[str, Any], planner_effective: str, executor_effective: str,
                  summary: str, files: Sequence[str] = (), passed: Sequence[str] = (), failed: Sequence[str] = (),
                  escalations: Sequence[str] = (), decisions: Sequence[str] = (), deviations: Sequence[str] = ()) -> None:
    excerpt_limit = settings["max_log_excerpt_kb"] * 1024
    def block(values: Sequence[str]) -> str:
        if not values:
            return "None."
        return bounded_text("\n".join(f"- {bounded_text(v, 2048)}" for v in values), excerpt_limit)
    notes = "Poor mode omits web search and applies policy constraints, but this CLI cannot guarantee OS-level network isolation." if settings["mode"] == "poor" else "None."
    text = f"""# Status
{status}
# Mode
{settings['mode']}
# Models
Planner requested: {settings['planner_model']}
Planner effective: {planner_effective}
Executor requested: {settings['executor_model']}
Executor effective: {executor_effective}
# Summary
{bounded_text(summary, excerpt_limit)}
# Files changed
{block(files)}
# Validation
## Commands run
{block(list(passed) + list(failed))}
## Passed
{block(passed)}
## Failed
{block(failed)}
# Escalations
{block(escalations)}
# Decisions needed from Chat
{block(decisions)}
# Deviations from TASK.md
{block(deviations)}
# Notes
{notes}
"""
    max_bytes = settings["max_result_kb"] * 1024
    if len(text.encode("utf-8")) > max_bytes:
        notice = "\n\nSome verbose runtime output was omitted to keep RESULT.md small.\n"
        text = text.encode("utf-8")[:max(0, max_bytes - len(notice.encode("utf-8")))].decode("utf-8", errors="ignore") + notice
    (root / ".ai/RESULT.md").write_text(text, encoding="utf-8")


def print_run_result(root: Path, status: str, settings: Dict[str, Any], planner: str, executor: str) -> None:
    label = "completed" if status == "completed" else "needs Chat" if status == "needs_chat" else "failed"
    print(f"AI workflow {label}")
    print("Result:")
    print(f"  {(root / '.ai/RESULT.md').resolve()}")
    if status in ("completed", "needs_chat"):
        print("Mode:")
        print(f"  {settings['mode']}")
        print("Planner:")
        print(f"  {planner}")
        print("Executor:")
        print(f"  {executor}")


def prompt_plan(settings: Dict[str, Any]) -> str:
    boundary = "No network, browsing, or external research. Return unresolved architecture/product choices as needs_chat." if settings["mode"] == "poor" else "External research is allowed only when justified by TASK.md."
    return (
        "PLANNING_PHASE: Inspect AGENTS.md, .ai/TASK.md, and only relevant repository files. Do not modify files. "
        "Return a concise execution plan matching the supplied JSON schema. Use status needs_chat for missing architecture/product requirements. " + boundary
    )


def prompt_execute(attempt: int, plan: Dict[str, Any], prior_error: str = "") -> str:
    return (
        f"EXECUTION_PHASE attempt {attempt}: Read AGENTS.md and .ai/TASK.md. Execute this existing plan; do not re-plan: "
        f"{json.dumps(plan, separators=(',', ':'))}. "
        "Respect path boundaries, preserve prior user work, run listed validation, and return the supplied JSON status. "
        "Do not commit, push, reset, clean, deploy, add major dependencies, or make unresolved architecture/product decisions. "
        + (f"Previous concise failure: {prior_error[-1200:]}" if prior_error else "")
    )


def run_workflow(root: Path, settings: Dict[str, Any], dry_run: bool, codex_bin: str, keep_history: bool = False) -> int:
    task_path = root / ".ai/TASK.md"
    adapter = CodexAdapter(root, settings, codex_bin)
    result_path = (root / ".ai/RESULT.md").resolve()
    if dry_run:
        try:
            validate_task(task_path, settings["mode"])
        except WorkflowError as exc:
            render_result(root, "failed", settings, "not invoked", "not invoked", str(exc), failed=[exc.kind])
            print_run_result(root, "failed", settings, "not invoked", "not invoked")
            return 2
        fake_runtime = Path(tempfile.gettempdir()) / "ai-codex.DRY_RUN"
        cmd = adapter.command(settings["planner_model"], settings["planner_reasoning"], prompt_plan(settings), fake_runtime / "execution-plan.json",
                              fake_runtime / "execution-plan.schema.json", True)
        print("Mode:", settings["mode"])
        print("Planner invocation:", " ".join(cmd))
        print("Executor requested:", settings["executor_model"])
        print("Executor fallbacks:", ", ".join(settings["executor_fallbacks"]))
        print("Executor reasoning:", settings["executor_reasoning"])
        print("Network policy:", "enabled" if settings["network"] else "disabled by policy; OS-level isolation unavailable")
        print("Result:")
        print(f"  {result_path}")
        return 0

    before = file_snapshot(root)
    planner_effective = executor_effective = "not invoked"
    escalations: List[str] = []
    try:
        archive_artifacts(root, settings, keep_history or settings["history_enabled"])
        task = validate_task(task_path, settings["mode"])
        with tempfile.TemporaryDirectory(prefix="ai-codex.") as temp:
            temp_path = Path(temp)
            (temp_path / "project.json").write_text(json.dumps({"project": str(root.resolve()), "pid": os.getpid()}), encoding="utf-8")
            plan_schema, execution_schema = write_runtime_schemas(temp_path)
            candidate = temp_path / "execution-plan.json"
            planner_models = [settings["planner_model"], *settings["planner_fallbacks"]]
            planner_effective, _ = adapter.invoke(planner_models, settings["planner_reasoning"], prompt_plan(settings), candidate,
                                                  plan_schema, True)
            plan = validate_plan(candidate)
            if plan["status"] == "needs_chat":
                decisions = [str(item) for item in plan["decisions_needed_from_chat"]]
                render_result(root, "needs_chat", settings, planner_effective, executor_effective,
                              "Planning stopped for decisions reserved for Chat.", decisions=decisions)
                print_run_result(root, "needs_chat", settings, planner_effective, executor_effective)
                return 3

            error = ""
            execution = None
            max_attempts = max(1, settings["max_repairs"] + 1)
            for attempt in range(1, max_attempts + 1):
                response = temp_path / f"execution-{attempt}.json"
                models = [settings["executor_model"], *settings["executor_fallbacks"]]
                try:
                    executor_effective, _ = adapter.invoke(models, settings["executor_reasoning"], prompt_execute(attempt, plan, error), response,
                                                            execution_schema)
                    try:
                        execution = json.loads(response.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError) as exc:
                        raise WorkflowError(f"Malformed executor response: {exc}", "failed_execution")
                    if execution.get("status") == "completed":
                        break
                    if execution.get("status") == "needs_chat":
                        decisions = [str(x) for x in execution.get("decisions_needed_from_chat", [])]
                        render_result(root, "needs_chat", settings, planner_effective, executor_effective,
                                      execution.get("summary", "Executor requires Chat input."), decisions=decisions)
                        print_run_result(root, "needs_chat", settings, planner_effective, executor_effective)
                        return 3
                    error = execution.get("error", execution.get("summary", "executor reported failure"))
                except WorkflowError as exc:
                    error = str(exc)
                if attempt >= settings["escalate_after"] and attempt < max_attempts:
                    packet = temp_path / "escalation.json"
                    changed = changed_files(before, file_snapshot(root))
                    packet.write_text(json.dumps({
                        "original_goal": task.get("Goal", "")[:800], "relevant_plan_step": plan["steps"][0] if plan["steps"] else {},
                        "files_involved": changed[:30], "what_was_attempted": f"executor attempts: {attempt}",
                        "exact_failing_command": "executor validation commands from plan", "important_error_output": error[-1600:],
                        "current_diff_summary": changed[:30], "decision_required": "localized diagnosis or revised mechanical plan"
                    }, indent=2), encoding="utf-8")
                    diagnosis = temp_path / f"diagnosis-{attempt}.json"
                    escalation_prompt = "ESCALATION_PHASE: Read .ai/TASK.md. Use this compact packet: " + bounded_text(packet.read_text(), settings["max_log_excerpt_kb"] * 1024) + ". Return a localized revised plan; do not edit files or restart broad analysis."
                    planner_effective, _ = adapter.invoke(planner_models, settings["planner_reasoning"], escalation_prompt, diagnosis,
                                                          plan_schema, True)
                    revised = validate_plan(diagnosis)
                    if revised["status"] == "needs_chat":
                        render_result(root, "needs_chat", settings, planner_effective, executor_effective,
                                      "Escalation identified a decision for Chat.", decisions=[str(x) for x in revised["decisions_needed_from_chat"]], escalations=[f"after attempt {attempt}"])
                        print_run_result(root, "needs_chat", settings, planner_effective, executor_effective)
                        return 3
                    plan = revised
                    escalations.append(f"Planner diagnosis after executor attempt {attempt}")
            if not execution or execution.get("status") != "completed":
                raise WorkflowError(error or "Executor exhausted bounded repair attempts", "failed_execution")

        after = file_snapshot(root)
        changed = changed_files(before, after)
        violations = scope_violations(changed, task)
        passed = [str(x) for x in execution.get("passed", [])]
        failed = [str(x) for x in execution.get("failed", [])]
        if violations:
            render_result(root, "failed", settings, planner_effective, executor_effective, "Scope validation failed; no automatic destructive rollback was attempted.",
                          changed, passed, failed, escalations, deviations=violations)
            print_run_result(root, "failed", settings, planner_effective, executor_effective)
            return 5
        render_result(root, "completed", settings, planner_effective, executor_effective,
                      execution.get("summary", "Execution completed."), changed, passed, failed, escalations)
        print_run_result(root, "completed", settings, planner_effective, executor_effective)
        return 0
    except WorkflowError as exc:
        render_result(root, "failed", settings, planner_effective, executor_effective, str(exc), changed_files(before, file_snapshot(root)), failed=[exc.kind], escalations=escalations)
        print_run_result(root, "failed", settings, planner_effective, executor_effective)
        return 4


def doctor(root: Path, settings: Dict[str, Any], codex_bin: str) -> int:
    resolved = shutil.which(codex_bin) if os.path.sep not in codex_bin else (codex_bin if Path(codex_bin).exists() else None)
    version = "unavailable"
    usable = "no" if not resolved else "unknown"
    if resolved:
        proc = subprocess.run([resolved, "--version"], text=True, capture_output=True)
        version = (proc.stdout or proc.stderr).strip().splitlines()[-1] if (proc.stdout or proc.stderr).strip() else "unknown"
        usable = "unknown" if proc.returncode == 0 else "no"
    git = subprocess.run(["git", "status", "--short"], cwd=root, text=True, capture_output=True)
    git_status = git.stdout.strip() or ("clean" if git.returncode == 0 else "not a Git repository")
    print(f"Codex executable: {resolved or 'not found'}")
    print(f"Codex version: {version}")
    print(f"Codex authenticated/usable: {usable}")
    print("Detected CLI capabilities: exec, per-invocation model/reasoning/sandbox/approval/search/output-schema")
    print(f"Project root: {root}")
    print(f"Configuration file: {config_path(root)}")
    print(f"Default mode: {settings['mode']}")
    print(f"Planner model: {settings['planner_model']}")
    print(f"Executor model: {settings['executor_model']}")
    print(f"Executor fallback: {', '.join(settings['executor_fallbacks'])}")
    print("Model availability: runtime detection")
    print(f"Git status: {git_status}")
    return 0 if resolved else 2


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def owned_runtime_dirs(root: Path, include_active: bool = True) -> List[Path]:
    owned = []
    for candidate in Path(tempfile.gettempdir()).glob("ai-codex.*"):
        marker = candidate / "project.json"
        if not candidate.is_dir() or not marker.is_file():
            continue
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
            if Path(data.get("project", "")).resolve() != root.resolve():
                continue
            pid = int(data.get("pid", 0))
            active = False
            if pid > 0:
                try:
                    os.kill(pid, 0)
                    active = True
                except (OSError, ValueError):
                    pass
            if include_active or not active:
                owned.append(candidate)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return sorted(owned)


def status_command(root: Path, settings: Dict[str, Any]) -> int:
    task = (root / ".ai/TASK.md").resolve()
    result = (root / ".ai/RESULT.md").resolve()
    runtimes = owned_runtime_dirs(root)
    history = history_dirs(root)
    print("Project:")
    print(f"  {root.resolve()}")
    print("Current task:")
    print(f"  {task if task.exists() else 'missing'}")
    print("Latest result:")
    print(f"  {result if result.exists() else 'missing'}")
    print("Latest result size:")
    print(f"  {human_size(result.stat().st_size) if result.exists() else 'n/a'}")
    print("Mode:")
    print(f"  {settings['mode']}")
    print("Temporary runtime files:")
    print("  none" if not runtimes else "  " + ", ".join(str(path) for path in runtimes))
    print("History:")
    state = "enabled" if settings["history_enabled"] else "disabled"
    print(f"  {state} ({len(history)} retained run{'s' if len(history) != 1 else ''})")
    return 0


def clean_candidates(root: Path) -> List[Tuple[str, Path]]:
    candidates: List[Tuple[str, Path]] = []
    old_plan = root / ".ai/EXECUTION_PLAN.json"
    if old_plan.exists():
        candidates.append(("stale execution plan", old_plan))
    history = root / ".ai/history"
    if history.exists():
        candidates.append(("workflow history", history))
    for pattern in ("*.log", "*.tmp"):
        for path in (root / ".ai").glob(pattern):
            candidates.append(("obsolete runtime log", path))
    for path in owned_runtime_dirs(root, include_active=False):
        candidates.append(("abandoned temporary runtime", path))
    return candidates


def clean_command(root: Path, dry_run: bool) -> int:
    candidates = clean_candidates(root)
    print("Workflow-owned cleanup categories:")
    print("  stale execution plans; history; runtime logs; abandoned project-marked temp directories")
    if not candidates:
        print("No cleanup candidates found.")
        return 0
    for category, path in candidates:
        action = "Would remove" if dry_run else "Removing"
        print(f"{action} [{category}]: {path.resolve()}")
        if not dry_run:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ai-codex",
        description="Quota-aware multi-model Codex workflow",
        epilog=("Common usage: ai-codex doctor; ai-codex run --dry-run; "
                "ai-codex run --normal; ai-codex run --poor. "
                "Use ai-codex status to locate artifacts and ai-codex clean --dry-run before cleanup. "
                "Environment: AI_MODE=normal|poor, AI_PLANNER_MODEL=<model>, "
                "AI_EXECUTOR_MODEL=<model>, CODEX_BIN=<path>. CLI mode flags override AI_MODE."),
    )
    sub = p.add_subparsers(dest="command")
    run = sub.add_parser("run", help="validate, plan, execute, and report")
    modes = run.add_mutually_exclusive_group()
    modes.add_argument("--normal", action="store_true", help="normal quota profile")
    modes.add_argument("--poor", action="store_true", help="quota-conservation profile")
    run.add_argument("--dry-run", action="store_true", help="resolve and print policy without invoking Codex")
    run.add_argument("--keep-history", action="store_true", help="retain bounded TASK/RESULT history for this run")
    sub.add_parser("doctor", help="inspect local CLI and configuration")
    sub.add_parser("status", help="show current workflow artifacts and storage state")
    clean = sub.add_parser("clean", help="remove only workflow-owned disposable artifacts")
    clean.add_argument("--dry-run", action="store_true", help="print cleanup candidates without deleting")
    vt = sub.add_parser("validate-task", help="validate TASK.md")
    vt.add_argument("path", nargs="?")
    vt.add_argument("--mode", choices=("normal", "poor"), default="normal")
    vp = sub.add_parser("validate-plan", help="validate a transient execution plan")
    vp.add_argument("path", help="path to execution-plan JSON")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parser().parse_args(argv)
    if not args.command:
        parser().print_help()
        return 0
    root = project_root()
    try:
        if args.command == "validate-task":
            validate_task(Path(args.path) if args.path else root / ".ai/TASK.md", args.mode)
            print("Task is valid")
            return 0
        if args.command == "validate-plan":
            validate_plan(Path(args.path))
            print("Plan is valid")
            return 0
        config = load_config(config_path(root))
        cli_mode = "normal" if getattr(args, "normal", False) else "poor" if getattr(args, "poor", False) else None
        settings = profile_settings(config, cli_mode, os.environ)
        codex_bin = os.environ.get("CODEX_BIN", "codex")
        if args.command == "doctor":
            return doctor(root, settings, codex_bin)
        if args.command == "status":
            return status_command(root, settings)
        if args.command == "clean":
            return clean_command(root, args.dry_run)
        return run_workflow(root, settings, args.dry_run, codex_bin, args.keep_history)
    except WorkflowError as exc:
        print(f"{exc.kind}: {exc}", file=sys.stderr)
        if args.command == "run":
            fallback = profile_settings({"version": 1}, cli_mode if 'cli_mode' in locals() else None, {})
            render_result(root, "failed", fallback, "not invoked", "not invoked", str(exc), failed=[exc.kind])
            print_run_result(root, "failed", fallback, "not invoked", "not invoked")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
