# Decisions

Architecture decision records, newest first. One entry per decision that would
otherwise need re-litigating. Epic: RC1-542; this file starts life in RC1-543.

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
