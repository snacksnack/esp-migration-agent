# Migration playbook

The runbook an FDE follows to run a customer email-platform migration with
this engine. Each stage names who does what, the command that runs it, the
abort point, and the observability checkpoint that proves it happened.
System view: [architecture.svg](architecture.svg) (editable source:
`architecture.drawio`).

The trust model holds at every stage: the LLM proposes, deterministic Python
validates, a human approves. The engine has no write path to the source, and
nothing downstream of the review gate reads anything except an approved spec.

## 0. Prerequisites and access

Before the engagement starts, confirm:

- **Source access**: an account-level export of the source ESP. For
  Mailchimp this is UI-triggered only (no API), limited to one export per
  24 hours, and requires a user with export rights - schedule it, do not
  assume it. Know the account's display timezone: export timestamps carry no
  offset and are rendered account-local (ADR-0004).
- **Target access**: a target account and API key with list/contact/campaign
  write scope. Dev rehearsals use Listmonk (`docker compose up -d`, then
  `eval "$(uv run python -m executor.bootstrap --export)"`); live targets
  plug in behind the same protocol via `--target`.
- **Workstation**: `uv sync`, then `uv run ruff check . && uv run pytest`
  green. Credentials go in `.env` (`ESP_` prefix, see `.env.example`); every
  credential is optional and nothing reads `.env` from tests.
- **Telemetry (optional but recommended on an engagement)**: `uv sync
  --group obs` and `DD_API_KEY` in the environment. Without them every
  telemetry call is a quiet no-op - the migration itself is unaffected.
- **Client roster**: a named spec owner (the person whose name goes on
  `approve --by`), and whoever owns consent/compliance questions.

## 1. Export capture

1. Trigger the account export in the source UI; download and unpack the
   bundle.
2. Parse it immediately: `uv run python -m discovery.report` runs ingest
   first and malformed input fails loudly with a named `IngestError` - a
   bad bundle costs you a 24-hour export cycle, so find out on day one.
3. Record the account timezone and pass `--account-tz` if it is not
   America/New_York.

Known source trap (found on the fixture, holds generally): the audience CSV
export can silently omit populations the account UI counts - the risk
register calls these out (e.g. pending/not-yet-confirmed members). Never
reconcile against the UI number; reconcile against what the bundle contains
and flag the gap to the client.

**Abort point**: bundle will not parse, or inventory is wildly off from what
the client described. Stop before scoping anything.

**Checkpoint**: `esp_migration.pipeline_run` count for the ingest/discovery
stage in Datadog.

## 2. Discovery review with the client

1. Run `uv run python -m discovery.report --narrate` and send the rendered
   report ahead of the meeting.
2. Walk the client through, in order: audience profile, inventory (lists,
   fields with observed formats, segments, templates, suppressions, consent
   states), the severity-ranked risk register, and the scope-and-estimate
   proposal with its effort drivers.
3. Decisions to extract from this meeting: consent semantics on the target
   (what each source status becomes), which segments are still wanted,
   what explicitly does not migrate, and the freeze-window placement.

Every number in the report is computed; the LLM only narrates. If the client
questions a number, trace it to the bundle, not to the prose.

**Abort point**: high-severity risks the client will not accept (e.g.
consent provenance gaps). Better to stop here than at reconciliation.

**Checkpoint**: the narrator trace in LLM Obs (ml_app
`esp-migration-agent`, version-tagged), plus the discovery-success SLO.

## 3. Mapping proposal and the approval gate

1. Propose: `uv run python -m mapping.review propose` writes a draft spec
   under `specs/drafts/`. Every mapping carries a confidence and rationale;
   anything ambiguous is `status: flagged` with an `open_question` - the
   proposer never guesses.
2. Review: `uv run python -m mapping.review status <spec>` lists what is
   proposed and what is open. Merge the flagged mappings into the risk
   conversation (`discovery.report --spec <spec>`).
3. Resolve: the client edits the YAML - the spec is the reviewable document.
   Each open question gets an explicit answer in the mapping itself.
4. Sign off: `uv run python -m mapping.review approve <spec> --by "<spec
   owner>"`. The approved spec moves under `specs/approved/` with the
   approval block and a pinned content sha256.

**What refusal looks like**: `approve` exits non-zero and lists the open
questions while any mapping is still flagged or unresolved - there is no
override flag, and that is the point. Downstream stages read only through
`mapping.gate.load_approved`, which re-verifies the hash: any post-approval
edit means re-approval, not a quiet re-run.

**Abort point**: open questions the client cannot answer. They stay open and
the migration waits; do not resolve them yourself to keep a date.

**Checkpoint**: `esp_migration.gate_approval` count; proposer trace in LLM
Obs. If the proposer or its prompt changed since the last engagement, run
the eval suite first (`uv run --group evals python -m evals` - billed,
never in CI) and check flag recall/precision before trusting a new draft.

## 4. Dry run

1. `uv run python -m executor.run --spec specs/approved/<spec>.yaml` -
   dry-run is the default; writes require `--execute`.
2. Review the diff with the client: creates and updates per phase
   (suppressions, subscribers, campaigns), and nothing unexpected. The plan
   builder re-checks the suppression invariant here; a hostile or edited
   spec raises before a single write.

**Abort point**: any unexpected diff line. Fix the spec (which means
re-approval), never the diff.

## 5. Execute

1. `uv run python -m executor.run --spec ... --execute [--target <t>]`.
2. Phase order is enforced by the executor, not by the operator:
   suppressions land non-subscribable first, then subscribers, then draft
   campaigns (nothing is sent).
3. Prove convergence: run the executor a second time and confirm it reports
   zero writes. Idempotent upserts make partial failure recoverable - a
   crashed run is re-run, not repaired by hand.

**Checkpoint**: `esp_migration.executor_writes` vs
`esp_migration.executor_write_errors`; the write-error monitor should stay
green through the run.

## 6. Reconciliation - acceptance criteria

`uv run python -m executor.reconcile --spec ...` produces the customer-facing
sign-off report. PASS requires all of:

- **Consent counts**: every consent state matches expected exactly, and the
  total matches.
- **Suppression integrity: 100%, enumerated.** Every suppressed address is
  individually verified non-subscribable in the target. 99.9% is a FAIL.
- **Segment parity**: target counts match the plan. Source counts are
  reference only - the approved spec may deliberately change segment
  semantics.
- **Sampled checksums**: sampled subscribers match field-for-field.
- **Campaign translation**: zero residual source merge tags, rendered output
  matches.

Any FAIL stops the cutover clock. Re-run reconciliation daily through the
parallel-run window; unsubscribes collected in the source re-sync by
re-running the executor (the suppression phase picks them up first).

**Abort point**: this is the formal go/no-go. No PASS, no cutover, no
exceptions negotiated in the meeting.

**Checkpoint**: `esp_migration.reconcile_result` and the
reconciliation-pass SLO; archive the report with the engagement record.

## 7. Cutover

Generate the plan from the approved spec rather than writing it by hand:
`uv run python -m executor.cutover --spec ... --out cutover_plan.md`. The
committed [cutover_plan.md](cutover_plan.md) is a worked example. In brief:

1. Announce a 24-hour freeze; pull a fresh export at freeze start; re-run
   discovery and diff counts against the plan's scope.
2. Final delta pass: re-run the executor against the fresh export - upserts
   converge, so only freeze-window deltas write.
3. Repoint signup forms and integrations to the target.
4. First send per the ramp in the plan, with bounce/complaint monitoring
   live; target sending stays OFF until the parallel-run reconciliation has
   passed.
5. Source account goes read-only, retained for the retention period.

**Checkpoint**: tag the release (`v*`) that ran the migration - `release.yml`
turns it into a Datadog deployment event, so the engagement shows up in
DORA with everything else.

## Rollback and abort points

The source is never written - the executor has no write path to it - so the
source remains a complete, untouched system of record until it is retired.
That makes aborting cheap at every stage before cutover:

| Stage | Abort action | What is at risk |
| --- | --- | --- |
| 1-3 (pre-approval) | Stop; nothing has been written anywhere | Nothing |
| 4 (dry run) | Stop; dry run writes nothing | Nothing |
| 5-6 (execute, parallel run) | Repoint nothing; wipe the target list; re-run later | Suppression events collected only in the target must be exported back to the source before the wipe - this is the one data path to preserve |
| 7 (post-cutover) | Repoint forms and sending back to the source; wipe the target; schedule a re-run | Mail already sent from the target, and the sender-reputation signals those sends generated, cannot be rolled back |

The irreversibility boundary is the first send from the target. Everything
before it is recoverable; that is why sending stays off until reconciliation
passes.

## Observability checkpoints, by stage

| Stage | Signal | Where |
| --- | --- | --- |
| Ingest/discovery | `esp_migration.pipeline_run`; narrator trace | Datadog metrics; LLM Obs |
| Proposal | proposer trace; eval trend after proposer changes | LLM Obs; eval store (`esp-mapping-quality`) |
| Approval | `esp_migration.gate_approval` | Datadog metrics |
| Execute | `executor_writes` / `executor_write_errors`; write-error monitor | Datadog metrics + monitor |
| Reconcile | `reconcile_result`; reconciliation-pass SLO | Datadog metrics + SLO |
| Release | deployment event from the `v*` tag | DORA |

All of it is as-code in `tpm-automation-platform/datadog/` (dashboard,
monitors, SLOs) with daily drift CI; none of it was clicked into the UI.
