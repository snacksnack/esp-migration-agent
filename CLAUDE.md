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
- `ingest/` - deterministic Mailchimp export-bundle parsers + typed models
  (ADR-0004). Export timestamps are account-local; parsers convert to UTC.
- `discovery/` - audience profile, inventory, risk register, proposal, and
  the rendered report (`python -m discovery.report`). The only LLM call is
  `narrator.py`, which narrates precomputed aggregates and nothing else.
- `mapping/` - the Phase 2 proposer + review gate (ADR-0005). `proposer.py`
  holds the system's main LLM call; its payload is its entire world, Python
  assembles the spec shell, and pre-resolved output is a ProposalError.
  `gate.py::load_approved` is the ONLY door downstream stages read a spec
  through. The review CLI is `python -m mapping.review`.
- `executor/` - Phases 3-4 (ADR-0006/0007): plan builder, target adapters
  (Listmonk dev, Brevo live demo) behind one protocol picked by --target,
  diff/execute engine, reconciliation, cutover plan, dev-target bootstrap.
  The suppression invariant is checked at plan build, per write, and at
  reconciliation (100%, enumerated). Dry-run is the default
  (`python -m executor.run --spec ...`); writes need --execute. The spec is
  canonical: platform differences live in adapters, translated on write and
  reversed on read. Integration tests (`pytest -m integration`) read env
  from the process only, never .env; listmonk e2e runs in CI, brevo is a
  manual demo asset.
- `scripts/demo.sh` + `docs/demo.md` - the recorded two-minute demo arc.
- `telemetry/` - LLM Obs tracing + esp_migration.* run metrics (ADR-0008).
  Decoration by contract: no ddtrace (`uv sync --group obs`) or no DD_API_KEY
  in the process env means quiet no-ops; a failed POST is loud on stderr but
  never raises. ml_app `esp-migration-agent`; DD_VERSION defaults to the
  pyproject version so root-span tags index (RC1-407). A pushed v* tag is the
  release (`release.yml`): gates, then DORA deployment/failure events.
- `evals/` - the agent-evals subject `esp-mapping-quality` (RC1-548): frozen
  proposer payloads in `evals/cases/`, ground truth in `expectations.py`
  (must_flag seeded ambiguities / must_propose with target predicates /
  advisory judgment calls). `uv run --group evals python -m evals` is BILLED
  and records to the shared store; the scorer itself is tested free in
  tests/test_evals_subject.py. Publish the trend page after a live run.
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
