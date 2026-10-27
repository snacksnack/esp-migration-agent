# Engagement writeup: Mailchimp → Brevo migration

Written the way a real engagement would be papered: the statement of work up
front, the acceptance report at the end, both signed against numbers the
engine computed rather than estimates anyone wrote down. The engine itself
is ESP-agnostic (the mapping spec is canonical; platforms live in adapters
behind one protocol); this document is one concrete engagement run through
it. The "customer" is the reference account this repository ships as its
fixture - a genuine Mailchimp export of a 4,500-member account (ADR-0001) -
migrated end to end into a live Brevo account. Every figure below traces to
a committed artifact.

## Statement of work

**Objective.** Migrate the customer's email program - audience, consent
states, suppressions, segments, and draft campaigns - from Mailchimp to
Brevo, with a signed-off mapping, a reconciled result, and no suppressed
address ever landing mailable in the target.

**In scope.** Audience with attributes and consent semantics; the full
suppression list; segment definitions re-expressed against the target;
draft campaigns with merge tags translated. Discovery report, mapping
specification, cutover plan, and reconciliation report as deliverables.

**Out of scope.** Sending (nothing is sent from the target during the
engagement); historical engagement data (opens/clicks do not migrate);
automation journeys; the source account's closure.

**Approach and gates.** The pipeline runs as a local CLI
([system view](architecture.svg), runbook: [playbook.md](playbook.md)).
Three gates hold regardless of schedule pressure: the mapping spec is
approved by a named owner and cannot be approved with open questions; the
executor's first run is a dry-run diff reviewed before any write; cutover
waits on a reconciliation PASS. The LLM proposes mappings and narrates
reports; every number is computed in Python; writes happen only from the
sha256-pinned approved spec.

## What discovery found

The export contained 4,500 members, of which 4,255 are exportable
(1 rejected by the source, 244 pending members silently dropped by
Mailchimp's own audience export - caught by the risk register and flagged
to the client rather than discovered at reconciliation). Timestamps are
rendered account-local with no offset; parsers convert to UTC. Member
ratings were uniform (no send history), which later shaped the cutover
sending ramp: by signup recency, not engagement cohort.

The mapping proposal covered the inventory with 7 mappings proposed and 9
flagged as open questions - zero guesses. All three deliberately seeded
ambiguities (mixed-format ZIPs, a purchase-date field mixing formats and
free text, and a near-duplicate VIP segment) were flagged, not resolved
silently; they were settled with
the client in review and the spec approved by name
(ADR-0005/0006 record the calls: LASTPURCH stays text, unsubscribed maps
to blocklisted, non-subscribed lands enabled/unconfirmed - not mailable on
Brevo - and both VIP segments migrate separately).

## Acceptance report

Acceptance required a reconciliation PASS on every criterion. Result on the
live Brevo run ([full report](reconciliation_report_brevo.md)):

| Criterion | Required | Measured | Result |
| --- | --- | --- | --- |
| Consent counts | exact, per state | 4,255 of 4,255; blocklisted 411, confirmed 3,756, unconfirmed 88 | PASS |
| Suppression integrity | 100%, enumerated | 411 of 411 non-subscribable in the target | PASS |
| Segment parity | target matches plan | 4 of 4, including VIP 1,709 and VIP legacy 1,712 | PASS |
| Sampled checksums | field-for-field match | 51 of 51 subscribers | PASS |
| Campaign translation | zero residual source tags, renders match | 3 of 3 campaigns | PASS |
| Idempotency | second run converges | zero writes on the second executor pass | PASS |

The same executor ran first against a self-hosted Listmonk target behind
the identical adapter protocol ([report](reconciliation_report.md)), which
is how the engine rehearses before touching a customer's live target.

What each line verified, precisely: segments are re-created as target-dialect
queries and checked for COUNT parity against the plan (the same predicate
evaluated locally over every planned record), not for pixel-identical segment
UIs; campaign translation verifies zero residual source merge tags plus a
render comparison with both sides substituted from the same sample values -
no mail is sent. Suppressed addresses are enumerated one by one, never
sampled. If the destination already held an address in a stricter consent
state than the plan, the executor keeps the stricter state and lists the
address as a conflict rather than downgrading protection (ADR-0013); this
engagement migrated into a fresh list, so no conflicts arose.

## What the engagement cost to run

Measured, not estimated, because every stage is traced: the LLM mapping
proposal cost $0.151 per run; the full write phase against Brevo took about
15 minutes for 4,255 contacts; the quality eval that gates proposer changes
costs $0.235 per billed suite run. The expensive part of a migration is the
judgment at the gates, and that is exactly the part this engine refuses to
automate.

## Rollback posture

The engine has no write path to the source, so Mailchimp remained a
complete, untouched system of record throughout: rollback before first send
is repoint-and-wipe, with target-collected suppressions exported back as
the one preserved data path. The irreversibility boundary is the first send
from the target, and sending stayed off until the acceptance table above
was green ([cutover plan](cutover_plan.md)).

## Live run, recorded

A scripted end-to-end demo of this engagement (discovery through
reconciliation) is committed as [demo.md](demo.md) with a two-minute
recording, runnable against a fresh target with `scripts/demo.sh`.
