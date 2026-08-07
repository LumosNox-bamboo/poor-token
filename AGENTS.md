# Repository instructions

## Quota-aware Codex workflow

The source of truth for a workflow run is `.ai/TASK.md`. Codex is primarily the
repository execution agent. Prefer action over commentary and keep artifacts
concise.

Codex may inspect repository files, understand the implementation, make the
explicitly requested changes, run tests/lint/typecheck/build, diagnose failures
for the current task, debug locally, and make minimal implementation decisions
required by an already-approved design.

Codex must not autonomously redefine requirements, expand scope, perform
unrelated refactors, choose between materially different architectures, select
new infrastructure, introduce major dependencies, change public APIs or
persistent data models without authorization, or turn implementation into a
technology evaluation. In poor mode it must not conduct broad research or
browse for alternative architectures.

When such a decision is necessary, stop that branch and record it in
`.ai/RESULT.md` under `## Decisions needed from Chat`. Respect Allowed paths and
Forbidden paths as hard boundaries. Never overwrite pre-existing user changes,
and never automatically commit, push, reset, clean, deploy, or mutate production.

Use `./scripts/ai-codex status` to locate the current task/result. Execution
plans, model responses, escalation packets, and validation logs are transient OS
temporary files. History is off by default; `run --keep-history` retains only a
bounded TASK/RESULT history. `clean --dry-run` previews conservative cleanup.
