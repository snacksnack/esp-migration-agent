# Fixtures

**Everything in this directory is synthetic.** The export bundle here is a
genuine Mailchimp account export, but the account was seeded exclusively with
Faker-generated data by `seeding/mailchimp_seeder.py` (fixed seed; ADR-0001).
No real subscriber data exists anywhere in this repository.

## Status (2026-10-25)

- `mailchimp_export/audience/` - CAPTURED: the genuine audience export
  (three CSVs: subscribed 3,756 / unsubscribed 411 / nonsubscribed 88 rows),
  matching `seeding/ground_truth.json` exactly.
- `api_supplement/` - CAPTURED via `seeding/capture_supplement.py`:
  saved-segment definitions (including conditions), template metadata, and
  draft-campaign metadata + HTML content. Mirrors real engagements, where
  segment logic is pulled over the API during discovery. The account owner's
  `reply_to` is masked to `owner@redacted.invalid` so nothing real is
  committed.
- `mailchimp_export/account/` - CAPTURED 2026-10-26 (Settings > Manage >
  Export account data; UI-only, one export per 24h). Lists mirror the
  audience export exactly: members 3,756 / transactional 88 / unsubscribed
  411 / cleaned 0, plus the three draft campaigns' content and both
  templates. `appointments.csv`, `events.csv` and `campaigns.csv` are
  headers-only on this account (no bookings, no tracked events, and
  campaigns.csv lists only sent campaigns - drafts appear solely under
  `campaigns_content/`).
- **OPTIN_IP is masked** across both export captures: Mailchimp recorded the
  seeding machine's real IP on every API-created member, so it is replaced
  with `203.0.113.35` (TEST-NET-3, same shape, parses identically) - the
  same posture as the masked `reply_to` in `api_supplement/`.

## Findings baked into the fixture (discovery-stage material)

- **Mailchimp's audience export silently omits pending members.** 4,499
  members are in the account; the CSVs carry 4,255. The 244 members with
  status `pending` appear in no CSV. **The account-level export omits them
  too** (verified 2026-10-26): pending members survive no export path at
  all, which upgrades the risk from "use the right export" to "unconfirmed
  signups cannot leave Mailchimp as data." A genuine source-platform risk for the
  risk register: unconfirmed signups do not survive a CSV-export migration.
- **MEMBER_RATING is uniformly 2** (Mailchimp's default) across all 4,255
  exported rows - the account never sent anything, so no engagement signal
  exists. This is the engagement ground truth: the "Highly engaged (4-5
  stars)" segment is structurally empty, and the audience profile's
  engagement distribution is degenerate by design.
- One generated address was rejected by Mailchimp's validity heuristic and is
  therefore absent from the account (`seeding/rejected_emails.json`); ground
  truth is reconciled to the 4,499 that landed.

## Capture procedure

To reproduce from scratch:

1. Create a Mailchimp account (14-day trial, 5,000-contact tier; cancel after
   the export is captured - see ADR-0001).
2. Put the API key in `.env` as `ESP_MAILCHIMP_API_KEY` (never commit it).
3. `uv run python -m seeding.mailchimp_seeder --count 4500` - seeds the
   members, merge fields, and two saved segments, and writes
   `seeding/ground_truth.json` (reconciled over observed rejections).
4. `uv run python -m seeding.campaign_seeder` - seeds two merge-tag-rich
   templates and three DRAFT campaigns (nothing is ever sent).
5. `uv run python -m seeding.capture_supplement` - captures segment
   definitions, template and campaign metadata to `api_supplement/`.
6. In the Mailchimp UI: export the audience (Audience > Export Audience) and
   the account (Account > Settings > Manage > Export account data; no public
   API, one export per 24h). Extract under `mailchimp_export/` and commit
   together with `seeding/ground_truth.json`.

## Ground truth

`seeding/ground_truth.json` records the distributions actually generated
(domain mix, consent mix, signup-tenure cohorts, states, seeded data-quality
risks, and the suppressed-email set). RC1-544's audience profile and risk
register are tested against it.

Mailchimp computes `member_rating` from real activity, so the engagement
distribution cannot be seeded; its ground truth is captured from the export
itself at fixture-freeze time and recorded below when the bundle lands.

## Seeded risks (for the risk register)

- Role-based addresses (info@, postmaster@, ...) on corporate domains.
- Missing first names (~5%).
- Malformed zip codes: 4-digit values (lost leading zeros) and free text.
- `LASTPURCH` merge field mixing ISO dates, prose dates, and day-first
  formats.
- An engagement-based saved segment ("Highly engaged (4-5 stars)") that the
  Listmonk target cannot represent - the seeded unsupported-feature risk.

## Seeded mapping ambiguities (RC1-545)

Added 2026-10-26, before the trial closed, so Phase 2 has ground truth for
flag-don't-guess. The audience CSVs are untouched; only the API supplement
was re-captured (saved segments do not alter members).

- **Overlapping VIP segments.** "VIP customers" (1,709 members; LASTPURCH
  contains "2026") and "VIP (legacy)" (1,712 members; match ANY of the same
  purchase condition OR ZIP starts "112"). Both TextMerge-only, so each is
  representable in Listmonk - the open question is which is canonical and
  whether to consolidate, which no mapper may decide alone. Both also lean on
  the mixed-format LASTPURCH text field, so the text `contains` condition
  silently misses non-ISO rows (that is why the counts are not the full 2026
  purchaser population).
- **Mixed date formats** in `LASTPURCH` (seeded in RC1-543): normalize-on-
  migrate vs migrate-as-text is a human decision.
- **Unclear consent column:** the `nonsubscribed` bucket (88 members) has no
  self-evident equivalent in the target's consent model; mapping it is a
  judgment call about marketing-consent semantics.

## Synthetic breadth corpora (RC1-572, ADR-0017)

`synthetic/` holds corpora rendered from `seeding/generator.py` by
`seeding/bundle_writer.py` - **not** live captures. The writer is pinned to
the genuine capture above (header-for-header per bucket, account-timezone
timestamps, pending members omitted), so these exercise the same parser the
real export does. Never edit them by hand: change `seeding/build_corpora.py`
and run `uv run python -m seeding.build_corpora`; `tests/test_corpora.py`
fails if the committed files differ from a fresh build.

- `synthetic/custom_fields/` - one audience (374 / 33 / 14 exported) with two
  custom merge fields beyond the standard nine: `LOYALTY_TIER` (mixed
  casings, trailing padding, numeric tiers - a seeded ambiguity) and
  `POINTS` (uniform integers), plus a campaign using conditional and
  function merge tags (`IF:`/`ELSE:`/`END:IF`, `DATE:`).
- `synthetic/multi_audience/` - two audiences from one account
  (`audience_a/`, `audience_b/`) sharing one supplement whose campaigns
  target both. 15 addresses are unsubscribed in A and subscribed in B (the
  cross-audience consent conflict); 20 more are subscribed in both. The
  campaigns carry `recipients.list_name` ("Retail customers" for A,
  "Wholesale partners" for B) so each audience is proposed with
  `--audience` and migrated into its own list (RC1-573, ADR-0018).

Each corpus carries its own `ground_truth.json` (exported counts, seeded
ambiguities, overlaps, expected risk codes). Malformed exports are built per
test from these (`tests/test_malformed_exports.py`) and never committed.
