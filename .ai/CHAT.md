# Chat ↔ Codex Protocol

This file is the durable context for new ChatGPT conversations. Do not rely on a previous chat window knowing this workflow.

## Roles

ChatGPT owns work that benefits from conversation or broad reasoning:
- clarify requirements and acceptance criteria;
- research and compare external options when needed;
- make architecture, product, technology, and infrastructure decisions with the user;
- resolve ambiguous requirements;
- review Codex `RESULT.md` and decide what happens next.

Codex owns repository execution:
- inspect the repository and existing implementation;
- make explicitly requested code changes;
- run tests, lint, typecheck, and build;
- perform local debugging and mechanical repairs;
- make only minimal implementation choices inside an already-decided design.

ChatGPT does not need to manually route Sol/Luna/Terra. The local `ai-codex` workflow owns model routing.

## User commands

### `交给 Codex`
Prepare a complete Codex task. Default to normal mode unless the user specifies otherwise.

### `交给 Codex，poor`
Prepare a task suitable for quota-conservation mode. Resolve research, architecture, product, technology-selection, and ambiguous requirement questions in Chat before handing off.

### `交给 Codex，normal`
Prepare a task for normal mode. External research/network use may be permitted by the local workflow where supported.

### `继续 Codex 任务`
Read the supplied/project `RESULT.md`. Focus on `Decisions needed from Chat`, resolve those decisions with the user, then prepare the next task when appropriate.

### `审查 Codex 结果`
Read `RESULT.md` and assess whether the acceptance criteria were actually satisfied. Do not automatically create another task.

## TASK handoff format

When handing work to Codex, produce content suitable for `.ai/TASK.md` with these sections:

```markdown
# Goal

# Context

# Scope

## Allowed paths

## Forbidden paths

# Requirements

# Forbidden changes

# Acceptance criteria

# Validation commands

# Decisions already made

# Open questions
```

At minimum, Goal, Scope, Requirements, and Acceptance criteria must be concrete and non-empty.

For poor mode, do not leave architecture/product/research decisions unresolved. If they cannot be resolved, keep the task in Chat rather than sending it to Codex.

## RESULT handoff

Codex returns a concise `.ai/RESULT.md` containing status, effective mode/models, files changed, validation results, escalations, deviations, and any decisions needed from Chat.

If status is `needs_chat`, ChatGPT should solve only the blocking reasoning/decision and then produce an updated TASK. Do not ask Codex to make a prohibited product or architecture decision merely to keep execution moving.

## Storage policy

Keep persistent workflow state small. Actual projects normally retain only configuration plus current TASK/RESULT. Execution plans, model output, logs, escalation packets, and snapshots should be temporary and cleaned after the run. History is off by default and bounded when explicitly enabled.

Do not commit secrets, credentials, cookies, model transcripts, large logs, or project-specific runtime history to this template repository.