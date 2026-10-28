# Decisions

Architecture decision records, newest first. One entry per decision that would
otherwise need re-litigating. Epic: RC1-542; this file starts life in RC1-543.

## ADR-0019: Reconcile against the destination as the first run found it (2026-10-27)

**Decision.** RC1-574 (follow-up to ADR-0017): reconciliation no longer
assumes an empty destination when it can prove otherwise. The first
`--execute` journals a `baseline` event (ADR-0016's journal, not a new store)
after its first read and before its first write: every member of the list
with its consent, plus the account-wide status of planned addresses not on it
(the RC1-573 cross-audience case). Reconcile (`--journal`) reads the FIRST
baseline for that destination identity and list name and:

- leaves pre-existing members outside the plan out of membership (a contact
  added after the baseline is still unexpected);
- accepts a planned address landing stricter than planned as an expected
  deviation ONLY when the baseline shows it already protected and it landed
  protected with its global status as found - reported in its own section,
  never a failure;
- adds a check the plan alone cannot make: an address the baseline shows
  protected that is now mailable is a downgrade, and fails, even when that
  is exactly what the plan said;
- counts segments over the population that should be on the list (the plan,
  deviations as landed, pre-existing members as they are).

Suppression integrity never consults the baseline: 100%, unconditional.

**Only a pristine baseline counts.** A run after the migration has written
would record the migration's own records as pre-existing, so a baseline is
journaled only while no write to that destination list has ever been
journaled; a list migrated before RC1-574 never gets one, and reconcile says
so and falls back to the empty-destination checks. A baseline for another
list id (same name, recreated) does not apply. Without a journal (or with
`find_baseline` finding none) the behavior is exactly the old one.

**Why the journal.** It already binds a run to a destination identity
(ADR-0016) and is append-only; a baseline taken at reconcile time would see
the migration's writes, and one held in the spec would need the destination
to exist at review time, which it often does not.

**Consequences.** The baseline is one JSON line sized by the list; a very
large customer list makes a large first line, acceptable for an append-only
record read once per reconcile. Reconciling with a different journal file
than the run used reads as no baseline - loud (FAIL naming the pre-existing
addresses), never a false pass.

## ADR-0018: One audience per spec, per list; contacts are global (2026-10-27)

**Decision.** RC1-573 (follow-up to ADR-0017): each Mailchimp audience is
proposed, approved, and migrated on its own into its own destination list.
The spec records `source_audience` as provenance (bound by the approval hash;
unset is omitted, so pre-RC1-573 approvals keep their hash and their list
name `Imported audience (mailchimp)`). Propose resolves the audience from the
supplement's campaigns (`--audience`; inferred only when there is exactly
one); the plan refuses a spec without one when the supplement spans several,
or one the supplement never mentions, and scopes campaigns to it. The list is
`<audience> (imported from <source>)`.

**Probed against a live Listmonk (not assumed).** Contacts and their status
are global; subscriptions are per list. (1) Creating an address that exists
on any list answers 409 "E-mail already exists" - the pre-RC1-573 engine,
which only read the destination list, failed the second audience on its
first shared address (reproduced on the real API). (2) `PUT /subscribers/{id}`
REPLACES list memberships: the old update sent `lists:[this list]` and
silently dropped every other membership, a pre-populated target included.
(3) Bulk `PUT /subscribers/lists` action add attaches a membership without
touching the global status. (4) A PUT listing an existing membership leaves
that subscription as it is. (5) The subscriber `query` is spliced after the
list filter without parentheses: a segment `A OR B` counted B across the
whole account. Found by the e2e once a second list existed; the adapter now
wraps every segment query. Brevo's create is an upsert whose blacklist flag
would have un-suppressed a contact another audience suppressed.

**Mechanics.** Addresses not on the destination list are looked up account-
wide (`lookup_subscribers`; batched `IN` on Listmonk, paged walk on Brevo).
Found and protected while this spec wants it subscribable: new diff action
`attach` - membership only, consent untouched (Listmonk subscription
"unsubscribed", Brevo list add without the blacklist flag), reported as a
conflict like ADR-0013. Otherwise `update`, which re-sends every list the
record is on. Dry-run does the same lookup, so pending attaches show as
writes. A rerun converges: attached records are members, ordinary conflicts.

**Consequences.** Order matters in one direction only. A later audience's
suppression applies account-wide on Listmonk (the blocklist is global): an
address unsubscribed from A stops receiving B's mail too. That is the safe
direction; a reviewer who wants B to stay mailable maps unsubscribed to the
per-list `unsubscribed` label instead - a spec decision, not an executor
one. Reconciliation of the second audience still counts the kept
protections as checksum mismatches; teaching it expected deviations is
RC1-574.

## ADR-0017: Breadth corpora are synthetic, built, and pinned to the genuine capture's shape (2026-10-27)

**Decision.** RC1-572 (external review finding, GPT-6 Astra #12): the engine
was only ever exercised against one clean audience. The breadth corpora are
SYNTHETIC - rendered from `seeding/generator.py` by a bundle writer
(`seeding/bundle_writer.py`) - not further live Mailchimp captures. A genuine
capture is UI-triggered, one per 24h, and dies with the trial account; half
the scope (malformed exports, pre-populated targets) cannot be captured by
nature. ADR-0001's authority is preserved by pinning the writer to the
capture rather than to my reading of it: the writer's headers equal the
genuine export's header-for-header per bucket (a test caught that
unsubscribed carries UNSUB_* and NO LAST_CHANGED), timestamps round-trip
through the account timezone, and pending members are omitted exactly as
the real export omits them. The corpora are committed under
`fixtures/synthetic/` and must equal a fresh `python -m seeding.build_corpora`
byte for byte; malformed variants are built per test, never committed.

**Seed what exposes the limit; do not generalize past it.** Each corpus
surfaces a limit loudly instead of fixing it out of scope:

- *Custom merge fields* (LOYALTY_TIER mixed-format, POINTS uniform): ingest
  now reports every data-bearing column outside the standard export set
  (`MailchimpBundle.extra_columns`, raw sorted samples), discovery risks it
  HIGH (`unmigrated_custom_fields`), and the proposer payload carries it so
  `check_completeness` forbids silently dropping one. The executor still
  reads a fixed field set and refuses a spec that maps a custom field -
  migrating them is the source-generalization question (#10), still
  declined here.
- *Multiple audiences*: the parser reads one audience and refuses two in one
  folder; discovery risks `multiple_audiences` when campaigns target more
  than one list. Every audience plans into the same destination list name -
  pinned by test: the suppression invariant holds across audiences (an
  address unsubscribed in A stays protected when B says subscribed, per
  ADR-0013) and reconciliation fails naming exactly the other audience's
  addresses. Per-audience destination lists are follow-up RC1-573.
- *Pre-populated targets* (fixture scale: 300 stricter, 200 laxer, 150
  unrelated): protection is kept and re-reported every run, every
  suppression lands, unrelated contacts are untouched. Reconciliation
  assumes the list started empty and fails naming exactly the pre-existing
  addresses and the kept conflicts - precise, never silent. Baselining
  pre-existing members is follow-up RC1-574.
- *Malformed exports*: mid-row truncation, surplus cells, latin-1/UTF-16,
  semicolon re-saves, missing or truncated supplement files, headers-only
  and missing buckets all die with a named IngestError. Two findings: a
  truncation that cuts only a row's trailing metadata parsed silently (every
  REQUIRED field intact) - ingest now refuses any row whose cell count
  differs from the header's; and truncation exactly at a row boundary leaves
  a valid shorter CSV no parser can detect - discovery now compares the
  subscribed rows against the source's own recipient count for an
  unsegmented campaign (`export_row_count_mismatch`, HIGH). A UTF-8 BOM
  (Excel re-save) is accepted: the file is still UTF-8.
- *Conditional and function merge tags* (IF:/ELSE:/END:IF, DATE:): risked
  MEDIUM at discovery; the plan already refuses any untranslated tag.

**Evals.** The new ambiguity class - a custom field with mixed formats - got
the case `custom-fields` (frozen from the corpus, with a freshness test):
must_flag LOYALTY_TIER plus the generator's ZIP/LASTPURCH seedings,
must_propose POINTS as the precision twin. First billed run green on all
three cases ($0.431 / 179s); the ceiling re-baselined to ~2x that.

## ADR-0016: The journal records the run; the destination binds by record, not by approval (2026-10-27)

**Decision.** RC1-571 (external review finding, GPT-6 Astra #7): execution
left no durable record - the summary printed and was gone, so "the re-run
converges" was asserted from memory, never provable from artifacts. Every
`--execute` run now appends to an execution journal (`executor/journal.py`,
default `docs/execution_journal.jsonl`, `--journal` for engagements):
JSONL, one event per line, flushed as written, so a run killed mid-write
leaves a record that ends exactly where the run did and the resuming run
appends to the same file.

The journal records what the approval binds (spec path, content hash, source
fingerprint, parse timezone, approver) plus the one thing the approval
deliberately does NOT bind: destination identity (platform + host/account,
via `describe_destination()` on every adapter - Brevo names the account, the
host never varies). The re-review suggested hard-binding the destination at
approval time; rejected because the destination often does not exist when the
spec is reviewed (the engagement creates it at cutover). Recording it in the
run artifact answers the same audit question - "what ran, against what, by
whose sign-off" - without forcing reviewers to approve infrastructure that
is not there yet.

The journal is write-BEHIND: an event lands after its write completes, so a
crash between the two loses at most that one event. The journal therefore
understates, never overstates - it will never claim a write that did not
happen - and the idempotent re-run absorbs the gap (the committed artifact
shows exactly this: 2,836 journaled vs 2,837 landed in the interrupted run,
and the resume skipped all 2,837). Write-ahead was rejected: a record that
can assert un-landed writes is worse than one that can miss the last one.

Division of labor: the engine journals per-write events (phase, action, key,
`recovered: true` when an RC1-570 read-back stood in for a lost response, and
`conflict` for addresses kept at the destination's stricter consent per
ADR-0013); the CLI owns the run lifecycle (`run_start`, `plan`, `run_failed`
with the last error text, `run_complete`). Dry runs journal nothing - there
is nothing durable to record. The committed journal is a genuine record of an
interrupted run resumed to convergence against the dev Listmonk, staged the
same way the fixture seeds its risks: deliberately, so the artifact
demonstrates the property it exists to prove.

**Decision.** RC1-570 (external review finding, GPT-6 Astra #8): the adapters
raised `TargetError` on the first HTTP hiccup and the only recovery was the
idempotent re-run - correct, but needlessly loud for transient faults and
unsafe to improve with a blanket retry, because replaying a write whose reply
was lost is how duplicates are made. `executor/retries.py` now classifies
every failure by what it proves about the target's state, and both adapters
route all calls through it:

- **Safe (provably unprocessed):** 429 and 503, and connect-phase errors
  where the request never left the process. Bounded retries (4 attempts,
  0.5/1/2s backoff) for any method; `Retry-After` honored, capped at 30s so
  a hostile header cannot stall a run.
- **Ambiguous (the write may have landed):** read timeouts, dropped sockets,
  and gateway answers (502, 504). Idempotent calls replay (GET, PUT, and
  Brevo's `updateEnabled` contact upsert, declared `replay_safe`). Every
  other write verifies with a read-back before re-sending: Listmonk creates
  look the subscriber/list/campaign up by its natural key and, when found,
  the recovered row stands in for the lost response (follow-up writes use
  the recovered id). A non-idempotent write with no read-back refuses to
  replay and says so.
- **Fatal (understood and refused):** 400/401/403/404/500 and the rest raise
  immediately - expired credentials mid-run must stop the run, not burn the
  retry budget. The recovery from any partial run stays the idempotent
  re-run, which converges to zero writes (tests/test_interrupted_run.py).

The failure matrix is unit-level by design: `httpx.MockTransport` per adapter
(tests/test_failure_matrix_*.py) with injected transport and sleep, no
network, no real backoff waits. The e2e stays the happy path.

## ADR-0014 amendment (RC1-569, 2026-10-27): the parse timezone is an input
too. The fingerprint hashes the export FILES, but OPTIN_TIME/CONFIRM_TIME
are localized with --account-tz before converting to UTC, so the same files
parsed under a different timezone yield different target values. propose now
also stamps `source_timezone`; verify_source refuses a mismatched
--account-tz. Same compat rule: unset provenance fields are omitted from
hashing and YAML, so earlier signed specs keep their hash and run with a
loud note.

## ADR-0014: The approval binds the inputs, not just the spec text (2026-10-27)

**Decision.** RC1-566 (external review finding, GPT-6 Astra #5): the approval
pinned only the spec's content hash, so a signed spec could be executed
against a different export than the one reviewed, and `--target` could
disagree with `spec.target` (the plan builds with the spec's consent dialect
while the writes go to the flag's adapter). Now `mapping.review propose`
stamps `source_fingerprint` - a sha256 over every file of the export bundle,
computed by Python, never the model - into the draft; the approval's content
hash covers it; and both the executor and reconciliation refuse to run when
the bundle on disk fingerprints differently (`mapping.gate.verify_source`)
or when `--target != spec.target`. Pre-RC1-566 specs carry no fingerprint
and keep their original content hash (hashing and YAML omit the field when
unset, so committed approvals stay valid); they run with a loud stderr note
that bundle identity is not verified, rather than retroactively invalidating
signed artifacts.

## ADR-0013: The executor never downgrades stricter destination consent (2026-10-27)

**Decision.** RC1-568 (external review finding, GPT-6 Astra #6): the engine
was spec-wins - a contact who unsubscribed AT THE DESTINATION after the
export was taken would be "updated" right back to subscribable. Now, when
the existing target state is non-subscribable (by the same consent-dialect
table the plan writes with) and the plan wants the address subscribable,
the executor keeps the target's state, skips the write, and reports the
address as a conflict - in the dry-run diff, in the execution summary, and
on the CLI. Protection moves in one direction only: a plan that SUPPRESSES
an address always writes, a plan that would un-suppress one never does.
Unknown target states count as unprotected; only a state the dialect knows
is stricter may veto a write. Reconciliation stays strict on purpose: a
conflicted address shows up as a counts/checksum mismatch, so a FAIL report
plus the named conflict list is the artifact the humans resolve - the
executor does not pretend the plan converged when it deliberately did not.

**Deliverable boundary note (review finding #11).** The engagement doc now
states explicitly what each acceptance line verified: segments are
re-created as target queries and checked for count parity against the plan
(predicates evaluated locally), not pixel-identical membership UIs; campaign
translation verifies residual tags and a substituted render comparison, not
actual sends.

## ADR-0012: The demo reads public; only a successful sign-off is gated (2026-10-27)

**Decision.** RC1-562 amends ADR-0011's access model so the demo can be
linked from the portfolio: the SPA, the spec, and the report are public, and
the whole UI stays interactive - visitors open the review forms, type
targets, click every button. The gate's refusal is public too: approve runs
`mapping.gate.approve` first and returns the 409 verbatim to anyone, because
watching the gate refuse IS the demo. Writes are the approver's alone -
resolving and a successful sign-off answer 401 anonymously with an
explanatory message, which the UI presents as a demo-mode dialog ("anyone
can review, only the named owner signs") offering the approver sign-in and
retrying the clicked action. Nothing ever changes on disk for an anonymous
visitor. With no `ESP_REVIEW_PASSWORD` configured everything stays open
(local dev, CI).

Rationale: an earlier cut of this change made resolving a public write
(ephemerality as the safety net), but shared mutable state means one
visitor's edits vandalize or consume the demo for the next - the pristine
draft with its nine flagged ambiguities is the exhibit, and only the
approver should be able to change the exhibit. Resolve payloads keep length
caps as defense in depth. `approved_by` stays free text; real identity
remains RC1-554.

## ADR-0011: The hosted demo is ephemeral on purpose (2026-10-27)

**Decision.** RC1-561 hosts the review-gate UI at esp.hihelloreid.com (Fly
app `esp-migration-demo`, scale-to-zero) behind the school-search
shared-password gate, ported nearly verbatim: one password, an HMAC-signed
expiry cookie, stdlib only (`review_ui/auth.py`). With no
`ESP_REVIEW_PASSWORD` set the gate is off entirely, so local dev, CI, and
the tests are untouched. The SPA shell and assets are public; every `/api`
route except login and the auth probe requires the cookie; `/healthz` stays
open for the Fly check and the deploy boot gate.

**No volume, deliberately.** The image bakes the fixture bundle and the
committed draft spec; resolutions and approvals work against the container
filesystem and a machine restart (which scale-to-zero makes routine) resets
the demo to pristine. The alternative - a volume - would make the server's
disk the document of record, detached from git, which is the design the
local-first UI exists to avoid (ADR-0010). Nothing real is at stake in the
demo, so ephemerality is a feature: every visitor can approve the spec.

**Access, not identity.** The shared password protects who can see the demo;
`approved_by` stays free text. Real named-owner identity (SSO + audit log)
remains RC1-554.

**Deploys** ride `fly-deploy.yml` (school-search pattern): workflow_run on
CI success, boot-health gate (3x200 on /healthz, forcing a cold start on the
public URL), DORA deployment/failure events under service
`esp-migration-demo` - distinct from the repo's tag-driven releases. The
deploy gates on CI only, not the listmonk e2e: the demo never runs the
executor.

## ADR-0010: The review-gate UI is a front end, never a second gate (2026-10-27)

**Decision.** RC1-560 adds a browser surface for stage 4: a React/Vite app
(`review_ui/frontend/`) over a thin FastAPI backend (`review_ui/api.py`). The
backend wraps the existing gate functions and decides nothing itself:

- The draft YAML on disk stays the document of record. A resolution PATCH
  round-trips through `MappingSpec` validation and rewrites the file, so UI
  edits obey flag-don't-guess exactly as hand edits do (a flagged mapping
  cannot leave the form without a target - the schema refuses, not the UI).
- Approval is `mapping.gate.approve` with a named owner; the sha256 pin and
  the `specs/approved/` output are unchanged, and a refusal returns 409
  carrying the gate's own message, rendered verbatim in the UI - the browser
  equivalent of the CLI's non-zero exit.
- Discovery context comes from a `DiscoveryReport` JSON endpoint (the same
  model both report renders read), with the spec's open questions merged into
  the risk register exactly as `discovery.report --spec` does. Charts reuse
  the RC1-559 idiom and tokens: single-hue bars, values at the tips, status
  colors reserved for severity/status.
- A stale-index guard: each PATCH carries the mapping's source identity and
  is refused with 409 if the file changed underneath the browser.

Dev is two processes, reid_basic style: `python -m review_ui` on :8542 and
the Vite dev server proxying `/api`. A production build (`npm run build`) is
served by the backend alone from `frontend/dist`. FastAPI/uvicorn live in the
optional `ui` dependency group, like `obs`: the migration pipeline never
imports them. Frontend components follow reid_basic conventions (one
component per file, dedicated props interfaces, co-located CSS, local
useState); vitest runs as its own CI job. The demo arc (`scripts/demo.sh`)
stays CLI-only for now - the UI is demoed live, not recorded, until it earns
a beat there.

## ADR-0009: The eval measures flag discipline from both sides (2026-10-26)

**Decision.** The agent-evals subject `esp-mapping-quality` (RC1-548) runs the
shipped proposer call (`request_mapping`, same prompt/model/limits/validation)
against two frozen payloads and scores deterministically - no judge:

- **fixture-ambiguities** - the real fixture payload; the three seeded
  ambiguities (ZIP formats, LASTPURCH formats, the overlapping VIP segment)
  must come back flagged: flag *recall*.
- **clean-variant** - the same account with the ambiguities removed; ZIP and
  LASTPURCH must come back proposed: flag *precision*. A proposer that flags
  everything aces recall and is useless; only the pair measures the contract.

Ground truth is three-way (`evals/expectations.py`): must_flag, must_propose
(with deliberately loose target predicates - the concept is pinned, the
reviewer's attrib spelling is not), and judgment calls (MEMBER_RATING, the
consent-scope questions, segments inheriting flagged fields), which are
advisory and never gate - a flaky gate gets disabled within a week. Payloads
are frozen JSON, not rebuilt from the fixture, so a fixture change cannot
silently move the ruler. Published numbers (mapping_accuracy, flag_recall,
flag_precision) ride in each result's observations. Eval traces go to the same
ml_app under `service:evals`, excluded from the production cost monitor.

## ADR-0008: Observability is decoration; a tag is the release (2026-10-26)

**Decision.** Phase 5 (RC1-548) adds `telemetry/`: Datadog LLM Obs tracing for
the two model calls and `esp_migration.*` run metrics for everything the
service dashboard, monitors, and SLOs need. Three commitments:

- **Decoration, never load-bearing.** `ddtrace` lives in an optional `obs`
  dependency group (`uv sync --group obs` on a machine that should trace);
  with the group absent or `DD_API_KEY` unset, every telemetry call is a
  no-op and the pipeline behaves identically. The suite passes with neither.
  A failed metric POST prints to stderr and returns False - never raises
  (telemetry must not fail a migration) and never swallows silently (the v2
  series API answers a malformed payload with a 400 that would otherwise be
  indistinguishable from no traffic).
- **One ml_app, versioned root spans.** LLM Obs traces land under ml_app
  `esp-migration-agent` (one ml_app per agent: the LLM Obs app list is the
  fleet inventory). Pipeline stages are workflow spans (`discovery.report`,
  `mapping.propose`); the intake indexes a root span's tags only when the
  payload carries `version:` (RC1-407), so `enable_llmobs()` defaults
  DD_VERSION to the pyproject version. Cost per migration is derived from
  these traces. Eval runs pass `service="evals"` so the two populations
  stay separable.
- **Metrics name the ticket's questions.** `pipeline_run` (stage, outcome),
  `gate_approval` (approved/refused), `executor_writes` / `executor_write_
  errors` (target), `reconcile_result` (pass/fail) - counts, tagged
  service/env/version. Monitors, SLOs, and the dashboard are defined as code
  in tpm-automation-platform/datadog/ under the daily drift CI, never in the
  Datadog UI.

**Release = tag.** Nothing here deploys to infrastructure, so `release.yml`
adopts the agent-evals pattern (RC1-459): a pushed `v*` tag is the release,
gated on tag==pyproject version plus lint+tests, reported to Datadog DORA via
the shared scripts, announced through the estate's release-notify flow. That
is the standard Actions -> Datadog deploy path the ticket requires.

## ADR-0007: The spec is canonical; adapters own every platform difference (2026-10-26)

**Decision.** Phase 4 adds `executor/brevo.py` behind the same TargetClient
protocol, selected by `--target` (`executor/clients.py`). The executor core -
plan, engine, reconciliation - does not change; the SAME approved spec drives
both targets. What the spec says (attrib paths, canonical consent labels, the
tiny query dialect, the plan's template syntax) is the canonical vocabulary;
rendering it into a platform is adapter work, reversed on read so the engine
always compares in one vocabulary:

- **Attributes:** `first_name` <-> Brevo's global `FIRST_NAME`; Brevo number
  attributes come back as floats and are re-normalized to the ints the plan
  wrote (padded zips stay text).
- **Merge tags:** a bijection (`TAG_TO_BREVO`) applied on write and inverted
  on read, so convergence checks and the reconciliation render comparison
  work unchanged. A canonical tag without a Brevo translation refuses loudly.
- **Consent:** Brevo has one control (global `emailBlacklisted`), no per-list
  subscription states. The canonical label persists in an `ESP_CONSENT`
  attribute; the flag lands per `BREVO_BLACKLISTED`. Unconfirmed contacts
  land NOT MAILABLE (retained, labeled, blacklisted until a re-permission
  pass): Brevo cannot represent "needs confirmation", and erring mailable
  would manufacture consent. On read the stored label is trusted only while
  the actual flag matches where that label lands; a contact un-blacklisted
  behind the executor's back reports its ACTUAL mailability, so suppression
  integrity fails loudly rather than trusting a stale label.
- **Segments:** Brevo has no server-side query API; `count_query` evaluates
  the spec's query dialect client-side (`executor/queries.py`, shared with
  reconciliation and the test fake).

**Demo** (`scripts/demo.sh`, runbook `docs/demo.md`): the full arc - export,
discovery, proposal with open questions, human review, approve, dry-run,
execute, reconciliation, cutover plan - against either target. `DEMO_FAST=1`
migrates a deterministic 600-subscriber sample so the arc fits a two-minute
clip; drafts only, nothing sends, on any target. The Brevo integration test
is env-gated and NOT in CI: the live account is a demo asset with real rate
limits.

## ADR-0006: The executor plans everything before writing anything (2026-10-26)

**Decision.** Phase 3 lives in `executor/`: a pure plan builder (`plan.py`),
a target protocol with the Listmonk adapter and an offline stand-in
(`target.py`, `listmonk.py`), diff + execution (`engine.py`), reconciliation
(`reconcile.py`), the cutover-plan artifact (`cutover.py`), and the dev-target
API-user bootstrap (`bootstrap.py`).

**The invariant is enforced three times, deliberately.** (1) `build_plan`
raises `SuppressionViolationError` while transforming - a hostile approved
spec that maps a suppressed bucket to a subscribable consent state never
yields a plan; the executor refuses rather than silently overriding, because
the reviewer must see the inconsistency. (2) The write loop re-checks per
record before every write. (3) Reconciliation enumerates every suppressed
address in the target - never sampled - and fails below 100%.

**Plan first, write second.** The full plan - every record transformed, every
consent label resolved against the target dialect - exists before the first
API call. Dry-run is the default; `--execute` is opt-in. Phases are
structural: suppressions are a separate list the write loop drains first.

**Idempotency is upsert-by-key plus convergence checks.** Subscribers key on
email, campaigns on name; a record matching the desired state (on the fields
the executor owns - target-side extras are ignored) is skipped. A second run
writes nothing; a crashed half-run converges on re-run. This is what makes
the delta pass and rollback-re-run in the cutover plan safe.

**Consent dialect lives with the executor** (per ADR-0002): the canonical
labels (`enabled/confirmed`, `enabled/unconfirmed`, `unsubscribed`,
`blocklisted`) map to Listmonk (status, subscription, subscribable) in
`CONSENT_DIALECTS`. `subscribable` is strict - unconfirmed counts as
subscribable for the invariant, because a confirm click is not suppression.

**Segments execute as verification queries, not target objects.** Listmonk
has no saved-segment entity; the approved queries are counted in
reconciliation against the same predicate evaluated in Python over the plan
(a deliberately tiny evaluator that refuses shapes it does not know). Source
counts are shown as reference only: the spec may deliberately change
semantics, and the report says which.

**Review resolutions for the fixture spec** (the 9 RC1-545 open questions,
resolved in `specs/drafts/mailchimp_to_listmonk.resolved.yaml` and approved):
ZIP zero-pads numerics (`zero_pad_us_zip`), non-numerics migrate as-is (the
risk register already carries them); LASTPURCH migrates as text (`attribs.
last_purchase` - the format question is answered by not parsing); MEMBER_
RATING freezes as `attribs.mc_member_rating`; Brooklyn keeps the zip LIKE;
"Highly engaged" recreates on the frozen rating (stays empty, matching the
source's 0); both VIP segments migrate separately with literal text-contains
semantics (consolidation is the customer's post-cutover call); subscribed ->
enabled/confirmed; unsubscribed -> blocklisted (global, safest); nonsubscribed
-> enabled/unconfirmed on a double-opt-in list (retained, never mailable
without confirm, and the import itself triggers no opt-in email).

**Dev-target auth.** Listmonk's API takes API-user tokens (the admin password
is login-only), so `executor/bootstrap.py` creates the API user once per
fresh container - locally via `--export`, in CI appended to `$GITHUB_ENV`.
The e2e job runs on every push: a migration engine whose end-to-end only runs
on demand is not tested.

## ADR-0005: The proposer only proposes; the gate owns resolution (2026-10-26)

**Decision.** Phase 2 lives in `mapping/`: the LLM proposer
(`mapping/proposer.py`), the review gate (`mapping/gate.py`), the
flagged-mapping risk bridge (`mapping/risks.py`), and the review CLI
(`python -m mapping.review`: propose / status / approve).

**The proposer's world is a deterministic payload** (`mapping_payload`), same
boundary as the discovery narrator: observed merge-field formats with
deterministic per-format sample values, full segment definitions, consent
buckets with counts, merge tags in use, and prose capability notes for the
target (`TARGET_NOTES`). The model returns only the four mapping lists;
Python assembles the spec shell (version, source, target, generated_at,
status=draft, suppression policy), so every structural field stays
deterministic. A proposer that emits status `approved`/`edited` is a defect
(`ProposalError`): only a human resolves a mapping.

**The review surface is the YAML document itself** (per ADR-0003): the
reviewer edits mappings to `approved`/`edited` and answers open questions in
place; `mapping.review status` renders the worklist. `approve()` refuses a
spec with any mapping still `proposed` or `flagged`, then stamps who/when and
a sha256 computed over the spec exactly as it will persist. Downstream stages
(RC1-546 executor onward) read specs only through `load_approved()`, which
re-derives the hash and refuses drafts and post-approval edits alike.

**Flagged mappings are register entries.** `spec_risks()` restates each
flagged mapping as a medium-severity `mapping_open_question` risk, and
`discovery.report --spec` merges them into the discovery register - one
severity-ranked log for the whole engagement, as RC1-545 specifies.

**Verified against the seeded ambiguities** (fixtures/README.md): the first
real proposer run flagged all three - LASTPURCH mixed date formats, the
overlapping VIP segments (asking exactly the consolidate-or-migrate-both
question), and the `nonsubscribed` consent bucket - plus the engagement
segment and ZIP quality issues, with zero guessed targets. Phase 5 turns this
into a repeatable eval.

## ADR-0004: Ingest and discovery are deterministic end to end; the LLM narrates last (2026-10-26)

**Decision.** Phase 1 splits into `ingest/` (parsers + typed bundle models) and
`discovery/` (profile, inventory, risk register, proposal, report). Everything
that counts, classifies, or detects is pure Python tested against the Phase 0
ground truth; the only LLM call is `discovery/narrator.py`, which receives the
computed aggregates - percentages precomputed - and drafts prose. The narration
payload is the narration's entire world, which is what Phase 5's evals check.

**Consent buckets are named as the source platform names them** (`subscribed /
unsubscribed / nonsubscribed`), not normalized at ingest. Normalization to the
target's consent model is a Phase 2 mapping decision a human approves; renaming
at parse time would hide exactly the decision the review gate exists for.

**Export timestamps are account-local.** Mailchimp renders audience-export
datetimes in the account's timezone with no offset in the value (verified
against the fixture's seeded UTC signups: exactly -4h/-5h, EDT/EST). Parsers
localize with the account timezone and convert to UTC; getting this wrong
shifts tenure-cohort boundaries, which is how it was caught - the profile test
against ground truth failed by 2 records at the lt_1y/1_3y boundary.

**Known platform gaps are register entries, not parser workarounds.** The
audience export omits pending members (244 in the fixture); the risk register
reports it as a high-severity reconcile-before-cutover item rather than the
parser inventing rows it cannot see.

**Effort model.** The proposal's per-workstream hours are transparent per-unit
constants stated inline in `discovery/proposal.py` - planning anchors to adjust
in review, which is how a real services estimate works. Every driver traces to
an inventory count (tested).

## ADR-0003: Mapping spec is YAML validated by pydantic models (2026-10-24)

**Decision.** The stage-4 reviewable document is a single YAML file validated
by pydantic models in `specs/model.py`. YAML because the customer reads and
edits it directly in the review gate (comments and diffs matter); pydantic
because the estate thesis puts every structural guarantee in deterministic
Python, not in the LLM or the reviewer's attention.

**Shape.** Top-level `MappingSpec` with `field_mappings`, `merge_tag_mappings`,
`segment_mappings`, `consent_mappings`, and a `suppressions` block. Every
LLM-proposed mapping carries `confidence`, `rationale`, and a `status`
(`proposed | approved | edited | flagged`); a flagged mapping must carry an
`open_question` and may omit its target - flag, do not guess.

**Invariants enforced by the model, not by review discipline:**

- A spec with `status: approved` must carry an `approval` block (who, when,
  sha256 of the reviewed content) and may contain no mapping still `proposed`
  or `flagged`. Nothing downstream (RC1-546 executor) accepts anything except
  an approved spec.
- `suppressions.order` is a `Literal["before_subscribers"]`. The suppression
  invariant (suppressions migrate first; no suppressed address can land
  subscribable) is unrepresentable any other way - the schema cannot express a
  spec that violates it.

**Worked example.** `specs/examples/mailchimp_to_listmonk.yaml`, validated in
CI by `tests/test_mapping_spec.py`.

## ADR-0002: Targets - Listmonk for dev, Brevo for the live demo (2026-10-24)

**Decision.** Dev/CI target is self-hosted Listmonk; the live-demo target is a
Brevo free account. SendGrid is ruled out.

**Why Listmonk for dev:** open source, single binary + Postgres, zero vendor
dependency, honest REST API with lists/subscribers/import endpoints - CI can
run a real end-to-end migration against a container without credentials or
rate limits.

**Why Brevo over SendGrid for live:** Twilio retired SendGrid's permanent free
plan in May 2025 (new accounts get a 60-day trial, then sending pauses and
contacts past 100 are deleted), so a SendGrid demo target would expire under
us. Brevo's free plan is ongoing: ~300 emails/day send cap, roughly 100K
contacts, and full API access to contacts, lists, attributes, and
transactional suppression - and a migration demo writes contacts, lists, and
suppressions while barely sending, so the send cap is irrelevant. Writes into
a third-party system we do not control are the point (FDE signal); Brevo also
has a genuinely different field/merge-tag/segment model than Mailchimp, which
keeps the mapping problem honest.

**Consequence.** The mapping-spec schema stays target-agnostic (`target` is a
string, per-target dialects live with the executor); RC1-546 builds against
Listmonk, RC1-547 adds the Brevo adapter.

## ADR-0001: Source fixture is a genuine export of a seeded Mailchimp account (2026-10-24)

**Decision.** The canonical source fixture is a real Mailchimp export bundle
pulled from an account that we first seed, via the Marketing API, with
Faker-generated data (fixed seed). Synthetic data only; the bundle is safe to
commit and lives in `fixtures/`.

**Account reality (updated 2026-10-25).** Mailchimp no longer has a permanent
free tier; the account is a 14-day trial on the 5,000-contact tier ($100/mo
after). The account is only needed for the one-shot seed + export - the
committed bundle is the permanent fixture - so the trial is canceled once the
export is captured. Seed count raised from 450 to 4,500 to use the headroom:
better demo numbers, same distributions.

**Why not fabricate the export format:** the parsers (RC1-544) are the
deterministic foundation of the whole pipeline; they must be tested against
what Mailchimp actually emits, not against our guess at it. Seeding real
Mailchimp and exporting gives real format, fully controlled data.

**Ground truth.** `seeding/generator.py` is a pure function: given a count and
a seed it returns subscriber records plus the expected aggregate distributions
(domain mix, geography, signup-tenure cohorts, consent mix, seeded
data-quality issues). Those expected aggregates are written to
`seeding/ground_truth.json` at seed time, and RC1-544's audience profile and
risk register are verified against them. ~4,500 subscribers (5,000-contact
trial tier, one audience).

**Seeded risks (so the risk register has ground truth):**

- Data quality: role-based addresses (info@, postmaster@), missing first
  names, inconsistent date formats in a free-text merge field, malformed zip
  codes. (Literally malformed email addresses cannot be seeded - the
  Mailchimp API rejects them - so the data-quality seedlings are field-level,
  which is also the more realistic failure mode.)
- Unsupported feature: at least one Mailchimp feature Listmonk cannot
  represent (candidate: a multi-condition segment on engagement signals, plus
  an automation/journey if the free tier allows creating one). Pinned
  precisely when the account is seeded; recorded in the fixture README.

**Engagement caveat.** Mailchimp computes `member_rating` from real activity;
it cannot be seeded directly. Ground truth for the engagement distribution is
therefore captured at fixture-freeze time from the export itself and recorded
in the fixture README alongside the seeded distributions.

**Mechanics.** Seeding is API-driven (`seeding/mailchimp_seeder.py`,
`ESP_MAILCHIMP_API_KEY`). The full account export bundle is UI-triggered
(Account > Settings > Manage > Export account data) - there is no public API
for it - so pulling the fixture is a documented manual step after seeding.
