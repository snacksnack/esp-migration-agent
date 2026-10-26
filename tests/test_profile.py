"""Audience profile vs the seeded ground truth (RC1-544 acceptance).

The expected aggregates are recomputed from the generator (seed 542) filtered
to what the export can contain: minus the one Mailchimp-rejected address and
minus pending members (Mailchimp audience exports omit them - a documented
fixture finding). That makes this an end-to-end check of the whole chain:
generator -> Mailchimp -> export CSVs -> parser -> profiler.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pytest

from discovery.profile import TOP_DOMAINS, ProfileError, build_profile
from ingest.mailchimp import load_bundle
from seeding.generator import generate

AUDIENCE = Path("fixtures/mailchimp_export/audience")
SUPPLEMENT = Path("fixtures/api_supplement")
AS_OF = datetime(2026, 10, 24, tzinfo=UTC)  # the generator's anchor date

BUCKET_FOR_STATUS = {
    "subscribed": "subscribed",
    "unsubscribed": "unsubscribed",
    "transactional": "nonsubscribed",
}


@pytest.fixture(scope="module")
def expected():
    rejected = set(json.loads(Path("seeding/rejected_emails.json").read_text()))
    subs, _ = generate(count=4500, seed=542)
    return [s for s in subs if s.email not in rejected and s.status != "pending"]


@pytest.fixture(scope="module")
def profile():
    return build_profile(load_bundle(AUDIENCE, SUPPLEMENT), as_of=AS_OF)


def test_total_and_consent_mix(profile, expected):
    assert profile.total == len(expected) == 4255
    want = Counter(BUCKET_FOR_STATUS[s.status] for s in expected)
    assert profile.consent == dict(want)


def test_domain_mix(profile, expected):
    assert profile.domain_class == dict(Counter(s.domain_class for s in expected))
    domains = Counter(s.email.split("@")[1] for s in expected)
    want_top = dict(sorted(domains.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_DOMAINS])
    assert profile.top_domains == want_top


def test_geography(profile, expected):
    assert profile.states == dict(Counter(s.state for s in expected))


def test_tenure_cohorts(profile, expected):
    assert profile.tenure_cohorts == dict(Counter(s.tenure_cohort for s in expected))


def test_engagement_is_uniform_rating_2(profile):
    # The account never sent anything, so Mailchimp's default rating holds
    # everywhere - the degenerate engagement signal the risk register flags.
    assert profile.member_rating == {2: 4255}


def test_suppressed_count(profile):
    truth = json.loads(Path("seeding/ground_truth.json").read_text())
    assert profile.suppressed == len(truth["suppressed_emails"]) == 411


def test_naive_as_of_fails_loudly(profile):
    with pytest.raises(ProfileError, match="timezone-aware"):
        build_profile(load_bundle(AUDIENCE, SUPPLEMENT), as_of=datetime(2026, 10, 24))
