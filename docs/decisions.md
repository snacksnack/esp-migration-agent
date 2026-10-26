# Decisions

Architecture decision records, newest first. One entry per decision that would
otherwise need re-litigating. Epic: RC1-542; this file starts life in RC1-543.

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
