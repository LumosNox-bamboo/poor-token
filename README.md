# poor-token

A small, quota-aware Chat ↔ Codex workflow that separates decisions from repository execution:

```text
Chat → TASK → Codex planner → cheaper executor → escalation when needed → RESULT → Chat
```

Chat clarifies requirements, performs external research, and owns architecture/product decisions. The configured planner briefly turns an approved task into an operational plan. The configured executor performs routine edits, tests, and bounded repairs. The workflow handles model routing; users do not need to micromanage Sol/Luna/Terra.

## Modes

- `normal`: high planner reasoning and fuller Codex/network capability where supported.
- `poor`: conserves quota, disables research/search by policy, bounds retries, and returns unresolved reasoning decisions to Chat.

Model names and profile behavior live in `.ai/config.yaml`. This template falls back to `.ai/config.example.yaml`, so it works before local configuration is created. Copy the example when you want project-local changes:

```sh
cp .ai/config.example.yaml .ai/config.yaml
```

## Commands

```sh
./scripts/ai-codex doctor
./scripts/ai-codex status
./scripts/ai-codex run --dry-run
./scripts/ai-codex run --normal
./scripts/ai-codex run --poor
./scripts/ai-codex clean --dry-run
./scripts/ai-codex clean
```

Copy `.ai/TASK.example.md` to the ignored `.ai/TASK.md`, fill in the concrete task, then run the workflow. The absolute path to the ignored `.ai/RESULT.md` is printed at every terminal exit. Plans, schemas, model responses, escalation packets, and logs use unique OS temporary directories and are cleaned automatically. History is disabled by default and bounded when explicitly enabled with `run --keep-history`.

## Start in a new Chat

Use this prompt exactly:

> Read `.ai/CHAT.md` from `LumosNox-bamboo/poor-token`
> and follow that Chat ↔ Codex protocol for this project.

Then use `交给 Codex`, `交给 Codex，poor`, `交给 Codex，normal`, `继续 Codex 任务`, or `审查 Codex 结果` as described in `.ai/CHAT.md`.

Project-specific `.ai/TASK.md` and `.ai/RESULT.md` should normally remain local and ignored. Never commit credentials, model transcripts, runtime logs, or history.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

The suite uses a fake Codex executable and consumes no model quota.
