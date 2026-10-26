"""Parsers against the committed fixture, plus loud-failure cases (RC1-544)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ingest.mailchimp import IngestError, load_audience_csv, load_audience_dir, load_bundle
from ingest.models import ConsentBucket

AUDIENCE = Path("fixtures/mailchimp_export/audience")
SUPPLEMENT = Path("fixtures/api_supplement")


@pytest.fixture(scope="module")
def bundle():
    return load_bundle(AUDIENCE, SUPPLEMENT)


def test_bucket_counts_match_fixture(bundle):
    assert len(bundle.members[ConsentBucket.SUBSCRIBED]) == 3756
    assert len(bundle.members[ConsentBucket.UNSUBSCRIBED]) == 411
    assert len(bundle.members[ConsentBucket.NONSUBSCRIBED]) == 88
    assert len(bundle.all_members) == 4255


def test_suppressed_is_the_unsubscribed_bucket(bundle):
    assert len(bundle.suppressed) == 411
    assert all(m.bucket == ConsentBucket.UNSUBSCRIBED for m in bundle.suppressed)


def test_supplement_objects(bundle):
    assert {s.name for s in bundle.segments} == {
        "Brooklyn subscribers",
        "Highly engaged (4-5 stars)",
        "VIP customers",  # RC1-545 seeded ambiguity: overlapping VIP segments
        "VIP (legacy)",
    }
    assert all(s.conditions for s in bundle.segments)  # definitions, not just names
    assert {t.name for t in bundle.templates} == {"Monthly newsletter", "Promo blast"}
    assert len(bundle.campaigns) == 3
    assert all(c.status == "save" for c in bundle.campaigns)  # drafts only, never sent


def test_campaign_merge_tags_extracted(bundle):
    tags = {tag for c in bundle.campaigns for tag in c.merge_tags}
    assert {"FNAME", "UNSUB", "LASTPURCH", "ZIP"} <= tags


def test_segment_campaign_links_saved_segment(bundle):
    by_title = {c.title: c for c in bundle.campaigns}
    segment_ids = {s.name: s.id for s in bundle.segments}
    assert by_title["Brooklyn free delivery"].saved_segment_id == segment_ids[
        "Brooklyn subscribers"
    ]
    assert by_title["October newsletter"].saved_segment_id is None


def test_missing_column_fails_loudly(tmp_path):
    bad = tmp_path / "subscribed_x.csv"
    bad.write_text('"First Name","Last Name"\nA,B\n')
    with pytest.raises(IngestError, match="Email Address"):
        load_audience_csv(bad, ConsentBucket.SUBSCRIBED)


def test_bad_member_rating_fails_loudly(tmp_path):
    bad = tmp_path / "subscribed_x.csv"
    header = ",".join(f'"{c}"' for c in [
        "Email Address", "First Name", "Last Name", "Zip code", "State",
        "Last purchase", "MEMBER_RATING", "OPTIN_TIME", "CONFIRM_TIME",
    ])
    bad.write_text(f"{header}\na@b.com,A,B,11201,NY,2026-01-01,not_a_number,,\n")
    with pytest.raises(IngestError, match="MEMBER_RATING"):
        load_audience_csv(bad, ConsentBucket.SUBSCRIBED)


def test_empty_audience_dir_fails_loudly(tmp_path):
    with pytest.raises(IngestError, match="expected exactly one"):
        load_audience_dir(tmp_path)
