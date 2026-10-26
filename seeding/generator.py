"""Deterministic subscriber generator (ADR-0001).

Pure: no I/O, no network. Given a count and a seed it returns the records to
load into Mailchimp plus the expected aggregate distributions, computed from
the records actually generated. Those aggregates are the ground truth that
RC1-544's audience profile and risk register are verified against.

Statuses are limited to what the Mailchimp API accepts on write (subscribed,
unsubscribed, pending, transactional); "cleaned" only ever comes from real
bounces, so the seeded suppression ground truth is the unsubscribed set.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from faker import Faker
from pydantic import BaseModel

CONSUMER_SHARE = 0.65
CONSUMER_DOMAINS = {"gmail.com": 40, "yahoo.com": 15, "hotmail.com": 6, "outlook.com": 4}

CONSENT_MIX = {"subscribed": 0.84, "unsubscribed": 0.09, "pending": 0.05, "transactional": 0.02}

# cohort name -> (min_days, max_days) since signup
TENURE_COHORTS = {
    "lt_1y": (7, 364),
    "1_3y": (365, 3 * 365 - 1),
    "3_7y": (3 * 365, 7 * 365 - 1),
    "gt_7y": (7 * 365, 10 * 365),
}
TENURE_WEIGHTS = {"lt_1y": 0.30, "1_3y": 0.40, "3_7y": 0.25, "gt_7y": 0.05}

STATE_WEIGHTS = {"NY": 0.35, "NJ": 0.15, "CA": 0.12, "TX": 0.08, "FL": 0.06, "OTHER": 0.24}

# Seeded data-quality issues (the risk register's ground truth).
ROLE_ADDRESS_RATE = 0.02  # info@, postmaster@, ... on corporate domains
MISSING_FNAME_RATE = 0.05
BAD_ZIP_RATE = 0.02  # 4-digit (lost leading zero) or free text
MESSY_DATE_RATE = 0.12  # LAST_PURCH in a non-ISO format or prose

ROLE_LOCALPARTS = ["info", "postmaster", "admin", "sales", "support"]
MESSY_DATE_FORMATS = ["%b %d %Y", "%d/%m/%Y", "%m-%d-%y"]
PROSE_DATES = ["last spring", "around the holidays", "n/a"]


class Subscriber(BaseModel):
    email: str
    status: str
    fname: str
    lname: str
    zip_code: str
    state: str
    signup_at: datetime
    last_purch: str
    tenure_cohort: str
    domain_class: str  # consumer | corporate
    seeded_risks: list[str] = []


class GroundTruth(BaseModel):
    """Expected aggregates, counted from the generated records."""

    seed: int
    count: int
    domain_class: dict[str, int]
    top_domains: dict[str, int]
    consent: dict[str, int]
    tenure_cohorts: dict[str, int]
    states: dict[str, int]
    risk_counts: dict[str, int]
    suppressed_emails: list[str]  # unsubscribed set: the suppression invariant's ground truth


def _weighted(rng: random.Random, weights: dict[str, float | int]) -> str:
    names = list(weights)
    return rng.choices(names, weights=[weights[n] for n in names], k=1)[0]


def generate(count: int = 450, seed: int = 542) -> tuple[list[Subscriber], GroundTruth]:
    rng = random.Random(seed)
    fake = Faker()
    fake.seed_instance(seed)
    now = datetime(2026, 10, 24, tzinfo=UTC)

    subs: list[Subscriber] = []
    seen_emails: set[str] = set()
    while len(subs) < count:
        risks: list[str] = []
        fname, lname = fake.first_name(), fake.last_name()

        domain_class = "consumer" if rng.random() < CONSUMER_SHARE else "corporate"
        if domain_class == "consumer":
            domain = _weighted(rng, CONSUMER_DOMAINS)
            local = f"{fname}.{lname}{rng.randint(1, 999)}".lower()
        else:
            domain = fake.domain_name()
            if rng.random() < ROLE_ADDRESS_RATE / (1 - CONSUMER_SHARE):
                local = rng.choice(ROLE_LOCALPARTS)
                risks.append("role_address")
            else:
                local = f"{fname[0]}{lname}".lower()
        email = f"{local}@{domain}".lower()
        if email in seen_emails:
            continue
        seen_emails.add(email)

        if rng.random() < MISSING_FNAME_RATE:
            fname = ""
            risks.append("missing_fname")

        zip_code = fake.zipcode()
        if rng.random() < BAD_ZIP_RATE:
            zip_code = zip_code.lstrip("0")[:4] if rng.random() < 0.5 else "ask reid"
            risks.append("bad_zip")

        cohort = _weighted(rng, TENURE_WEIGHTS)
        lo, hi = TENURE_COHORTS[cohort]
        signup_at = now - timedelta(days=rng.randint(lo, hi), minutes=rng.randint(0, 1439))

        purch_date = now - timedelta(days=rng.randint(1, 700))
        if rng.random() < MESSY_DATE_RATE:
            if rng.random() < 0.25:
                last_purch = rng.choice(PROSE_DATES)
            else:
                last_purch = purch_date.strftime(rng.choice(MESSY_DATE_FORMATS))
            risks.append("messy_date")
        else:
            last_purch = purch_date.strftime("%Y-%m-%d")

        state = _weighted(rng, STATE_WEIGHTS)
        if state == "OTHER":
            state = fake.state_abbr()

        subs.append(
            Subscriber(
                email=email,
                status=_weighted(rng, CONSENT_MIX),
                fname=fname,
                lname=lname,
                zip_code=zip_code,
                state=state,
                signup_at=signup_at,
                last_purch=last_purch,
                tenure_cohort=cohort,
                domain_class=domain_class,
                seeded_risks=risks,
            )
        )

    return subs, _aggregate(subs, seed)


def _count_by(subs: list[Subscriber], key) -> dict[str, int]:
    out: dict[str, int] = {}
    for s in subs:
        k = key(s)
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def _aggregate(subs: list[Subscriber], seed: int) -> GroundTruth:
    risk_counts: dict[str, int] = {}
    for s in subs:
        for r in s.seeded_risks:
            risk_counts[r] = risk_counts.get(r, 0) + 1
    return GroundTruth(
        seed=seed,
        count=len(subs),
        domain_class=_count_by(subs, lambda s: s.domain_class),
        top_domains=dict(
            list(_count_by(subs, lambda s: s.email.split("@")[1]).items())[:10]
        ),
        consent=_count_by(subs, lambda s: s.status),
        tenure_cohorts=_count_by(subs, lambda s: s.tenure_cohort),
        states=_count_by(subs, lambda s: s.state),
        risk_counts=dict(sorted(risk_counts.items())),
        suppressed_emails=sorted(s.email for s in subs if s.status == "unsubscribed"),
    )
