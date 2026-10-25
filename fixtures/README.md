# Fixtures

**Everything in this directory is synthetic.** The export bundle here is a
genuine Mailchimp account export, but the account was seeded exclusively with
Faker-generated data by `seeding/mailchimp_seeder.py` (fixed seed; ADR-0001).
No real subscriber data exists anywhere in this repository.

## Status

Not yet captured. The bundle lands here once the seeded account is exported
(RC1-543 acceptance). To produce it:

1. Create a Mailchimp free account (one audience, 500-contact cap).
2. Put the API key in `.env` as `ESP_MAILCHIMP_API_KEY` (never commit it).
3. `uv run python -m seeding.mailchimp_seeder` - seeds ~450 members, merge
   fields, and two saved segments, and writes `seeding/ground_truth.json`.
4. In the Mailchimp UI: Account > Settings > Manage > Export account data.
   (The full-account export has no public API; this step is manual.)
5. Download the zip, extract it into `fixtures/mailchimp_export/`, and commit
   it together with `seeding/ground_truth.json`.

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
