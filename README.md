# poor-token

A lightweight, quota-aware ChatGPT ↔ Codex workflow.

## Purpose

This repository stores the reusable protocol for splitting work between ChatGPT and Codex while keeping persistent files and disk usage small.

- **ChatGPT**: requirements, research, architecture/product decisions, complex reasoning.
- **Codex planner**: short repository-aware planning using the configured planner model.
- **Codex executor**: routine implementation, tests, lint/build, and mechanical repair using the configured executor model.
- **poor mode**: conserves Codex quota and sends research/architecture/ambiguous requirements back to Chat.
- **normal mode**: permits fuller Codex capabilities where supported.

## Start in a new Chat

Ask ChatGPT:

> Read `.ai/CHAT.md` in `LumosNox-bamboo/poor-token` and follow that Chat ↔ Codex protocol for this project.

Then discuss the task normally. When ready, say `交给 Codex，poor` or `交给 Codex，normal`.

## Runtime artifacts

Actual project runs should keep `TASK.md` and `RESULT.md` locally. Execution plans, logs, escalation packets, and other transient data should live in OS temporary storage and be cleaned after the run. History is disabled by default.

This template intentionally does not store per-run logs or histories.