#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

if "--version" in sys.argv:
    print("fake-codex 1.0")
    raise SystemExit(0)

args = sys.argv[1:]
model = args[args.index("-m") + 1]
output = Path(args[args.index("-o") + 1])
prompt = args[-1]
capture = os.environ.get("FAKE_CAPTURE")
if capture:
    with Path(capture).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"model": model, "args": args, "phase": prompt.split(":", 1)[0]}) + "\n")

if model in os.environ.get("FAKE_UNAVAILABLE_MODELS", "").split(","):
    print(f"model {model} unavailable", file=sys.stderr)
    raise SystemExit(1)

plan = {
    "version": 1, "status": "ready", "summary": "fake plan",
    "steps": [{"id": "step-1", "type": "edit", "description": "fake edit", "paths": ["fixture.txt"], "complexity": "mechanical", "preferred_executor": "executor"}],
    "validation": [{"command": "true", "purpose": "fake validation"}],
    "escalation_conditions": [], "decisions_needed_from_chat": []
}
if prompt.startswith("PLANNING_PHASE"):
    if os.environ.get("FAKE_NEEDS_CHAT") == "1":
        plan.update(status="needs_chat", steps=[], decisions_needed_from_chat=["Choose the persistence strategy."])
    output.write_text(json.dumps(plan), encoding="utf-8")
elif prompt.startswith("ESCALATION_PHASE"):
    plan["summary"] = "localized repair plan"
    output.write_text(json.dumps(plan), encoding="utf-8")
else:
    state = Path(os.environ.get("FAKE_STATE", str(output.parent / "state")))
    count = int(state.read_text() if state.exists() else "0") + 1
    state.write_text(str(count), encoding="utf-8")
    fail_count = int(os.environ.get("FAKE_FAIL_EXECUTIONS", "0"))
    if count <= fail_count:
        result = {"status": "failed", "summary": "mechanical test failure", "error": "test command failed", "passed": [], "failed": ["true"], "decisions_needed_from_chat": []}
    else:
        target = os.environ.get("FAKE_EDIT_PATH")
        if target:
            Path(target).write_text("created by fake executor\n", encoding="utf-8")
        result = {"status": "completed", "summary": "fake execution complete", "passed": ["true"], "failed": [], "decisions_needed_from_chat": []}
    output.write_text(json.dumps(result), encoding="utf-8")
