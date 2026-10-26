# CLAUDE.md

Guidance for Claude Code in this repository.

## What this is

ESP Migration Agent: gated email-platform migration engine (Mailchimp ->
Listmonk/Brevo). FDE portfolio project, epic RC1-542; template conventions
from school-search/launch-planner-agent. Estate thesis everywhere: the LLM
proposes, deterministic Python validates, a human approves.

## Non-negotiables

- The suppression invariant lives in Python and the schema, never in model
  behavior: suppressions migrate first; no suppressed address may land
  subscribable in the target. `SuppressionPolicy.order` is a Literal so a
  violating spec cannot even parse. Keep it unrepresentable.
- Flag, do not guess: an ambiguous mapping gets `status: flagged` plus an
  `open_question`, and may omit its target. This behavior is the piece under
  test in the Phase 5 evals; never "improve" it into a best-effort guess.
- Nothing downstream of the review gate reads anything except an approved
  spec (approval block + content hash, no proposed/flagged mappings left).
- Counting is deterministic. The LLM narrates computed aggregates and drafts
  prose; every number must trace to Python.

## Layout

- `specs/model.py` - mapping-spec schema (ADR-0003); worked example in
  `specs/examples/`, validated by tests.
- `seeding/generator.py` - pure, seeded subscriber generator returning records
  plus expected aggregates (the ground truth for RC1-544).
- `seeding/mailchimp_seeder.py` - API uploader; needs `ESP_MAILCHIMP_API_KEY`.
- `fixtures/` - synthetic Mailchimp export bundle + ground_truth.json (see
  fixtures/README.md for the capture procedure; the account export is
  UI-triggered, there is no API for it).
- `docs/decisions.md` - ADRs, newest first. Notable decisions go here, not in
  commit bodies.

## Conventions

- Config is env-backed pydantic-settings with the `ESP_` prefix, documented in
  `.env.example`. Every credential optional; everything must import and tests
  must pass with no .env present (tests never read .env).
- Malformed input fails loudly with a named error, never a silent zero
  (source-health convention).
- Imports at the top of the file; no function-local imports.
- Work under epic RC1-542; branches `rc1-NNN-slug`, commit subjects
  `RC1-NNN: what changed`, no attribution trailers. Reid merges PRs.

## Commands

```bash
uv sync && uv run ruff check . && uv run pytest   # same as CI
uv run python -m seeding.mailchimp_seeder --dry-run   # ground truth only
```

CI (`ci.yml`): ruff + pytest with --ddtrace (Test Optimization, agentless) +
--cov. Datadog monitors/SLOs/dashboard arrive with RC1-548 and live in
tpm-automation-platform/datadog/ as code, never clicked into the UI.
