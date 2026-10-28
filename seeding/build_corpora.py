"""Build the RC1-572 breadth corpora under fixtures/synthetic/ (ADR-0017).

    uv run python -m seeding.build_corpora

Deterministic: the committed corpora must equal a fresh build byte for byte
(tests/test_corpora.py enforces it), so edit THIS file, never the outputs.

- custom_fields: one audience carrying two custom merge fields beyond the
  standard nine - LOYALTY_TIER (mixed casings, padding and numeric tiers: a
  seeded ambiguity) and POINTS (uniform integers: one defensible answer) -
  plus a campaign using conditional and function merge tags.
- multi_audience: two audiences from one account with overlapping members,
  some of them unsubscribed in A and subscribed in B (the consent conflict a
  per-account migration has to face), and a supplement whose campaigns
  target both.

Malformed exports are built per test from these (tests/test_malformed_exports.py)
rather than committed: a broken file in fixtures/ is one accidental load away
from looking like data.
"""

from __future__ import annotations

import json
from pathlib import Path

from seeding.bundle_writer import (
    make_campaign,
    make_segment,
    write_audience_csvs,
    write_supplement,
)
from seeding.generator import Subscriber, generate

SYNTHETIC = Path("fixtures/synthetic")
CUSTOM_FIELDS = ("LOYALTY_TIER", "POINTS")
AUDIENCE_A, AUDIENCE_B = "Retail customers", "Wholesale partners"

LOYALTY_HTML = (
    "<p>*|IF:FNAME|*Hi *|FNAME|*,*|ELSE:|*Hi there,*|END:IF|*</p>"
    "<p>Your tier: *|LOYALTY_TIER|*. Points as of *|DATE:Y|*: *|POINTS|*.</p>"
    '<p><a href="*|UNSUB|*">Unsubscribe</a></p>'
)


def _exported(subs: list[Subscriber], status: str = "subscribed") -> list[Subscriber]:
    return [s for s in subs if s.status == status]


def _write_truth(path: Path, truth: dict) -> None:
    path.write_text(json.dumps(truth, indent=1, sort_keys=True) + "\n")


def build_custom_fields(out: Path) -> None:
    subs, truth = generate(450, seed=572, custom_fields=CUSTOM_FIELDS)
    list_id = "5720000001"
    subscribed = _exported(subs)
    write_audience_csvs(subs, out / "audience", export_id="572cf00001",
                        custom_fields=CUSTOM_FIELDS)
    ny = sum(1 for s in subscribed if s.state == "NY")
    write_supplement(
        out / "api_supplement",
        segments=[
            make_segment(57201, "New York members",
                         [{"condition_type": "TextMerge", "field": "STATE",
                           "op": "is", "value": "NY"}], ny),
        ],
        campaigns=[
            make_campaign("cf00000001", "Loyalty update", "Your loyalty status",
                          LOYALTY_HTML, list_id, len(subscribed),
                          list_name="Loyalty members"),
        ],
    )
    _write_truth(
        out / "ground_truth.json",
        {
            "corpus": "custom_fields",
            "generator": truth.model_dump(mode="json"),
            "exported_by_bucket": {
                "subscribed": len(subscribed),
                "unsubscribed": len(_exported(subs, "unsubscribed")),
                "nonsubscribed": len(_exported(subs, "transactional")),
            },
            "custom_fields": {
                field: sorted({s.custom[field] for s in subs if s.status != "pending"})
                for field in CUSTOM_FIELDS
            },
            "seeded_ambiguities": ["LOYALTY_TIER"],
            "unambiguous_custom_fields": ["POINTS"],
            "expected_risk_codes": ["complex_merge_tags", "unmigrated_custom_fields"],
        },
    )


def build_multi_audience(out: Path) -> None:
    a_subs, _ = generate(300, seed=5721)
    b_subs, _ = generate(300, seed=5722)
    b_emails = {s.email for s in b_subs}

    # Overlap: addresses on both audiences. The conflict half is
    # unsubscribed in A and subscribed in B - consent differs per audience.
    conflicts = [s for s in _exported(a_subs, "unsubscribed") if s.email not in b_emails][:15]
    shared = [s for s in _exported(a_subs) if s.email not in b_emails][:20]
    b_subs = b_subs + [s.model_copy(update={"status": "subscribed"}) for s in conflicts + shared]

    write_audience_csvs(a_subs, out / "audience_a", export_id="572ma0000a")
    write_audience_csvs(b_subs, out / "audience_b", export_id="572mb0000b")
    write_supplement(
        out / "api_supplement",
        campaigns=[
            make_campaign("ma0000000a", "Audience A news", "News for A",
                          "<p>Hi *|FNAME|*</p>", "572000000a", len(_exported(a_subs)),
                          list_name=AUDIENCE_A),
            make_campaign("mb0000000b", "Audience B news", "News for B",
                          "<p>Hi *|FNAME|*</p>", "572000000b", len(_exported(b_subs)),
                          list_name=AUDIENCE_B),
        ],
    )
    _write_truth(
        out / "ground_truth.json",
        {
            "corpus": "multi_audience",
            "audience_names": {"audience_a": AUDIENCE_A, "audience_b": AUDIENCE_B},
            "audience_a": {"subscribed": len(_exported(a_subs)),
                           "unsubscribed": len(_exported(a_subs, "unsubscribed"))},
            "audience_b": {"subscribed": len(_exported(b_subs)),
                           "unsubscribed": len(_exported(b_subs, "unsubscribed"))},
            "overlap_consent_conflicts": sorted(s.email for s in conflicts),
            "overlap_same_consent": sorted(s.email for s in shared),
            "expected_risk_codes": ["multiple_audiences"],
        },
    )


BUILDERS = {
    "custom_fields": build_custom_fields,
    "multi_audience": build_multi_audience,
}


def build_all(root: Path = SYNTHETIC) -> None:
    for name, builder in BUILDERS.items():
        builder(root / name)


if __name__ == "__main__":
    build_all()
    print(f"wrote {', '.join(BUILDERS)} under {SYNTHETIC}/")
