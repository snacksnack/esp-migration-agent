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
- Account-level export (Settings > Manage > Export account data) - PENDING:
  Mailchimp allows one export per 24h and the audience export used today's
  slot. Lands under `mailchimp_export/account/` tomorrow.

## Findings baked into the fixture (discovery-stage material)

- **Mailchimp's audience export silently omits pending members.** 4,499
  members are in the account; the CSVs carry 4,255. The 244 members with
  status `pending` appear in no CSV. A genuine source-platform risk for the
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
