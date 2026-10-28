"""Ground truth for the frozen proposer cases.

Every reviewable mapping the proposer must emit is one of three things here,
and the three are scored differently:

- **must_flag**: the seeded ambiguities. The fixture was built with these in
  it (RC1-543/545): ZIP mixes 5-digit, truncated and non-numeric values;
  LASTPURCH mixes ISO dates, an ambiguous second format and free text; and
  'VIP (legacy)' near-duplicates 'VIP customers' (1,712 vs 1,709 members,
  differing only by an OR on the Brooklyn rule). Proposing any of these is a
  guess, and a plausible guess is a defect - flag recall is the behavior
  under test.
- **must_propose**: mappings with one defensible answer. Flagging one of
  these is the other failure mode (a proposer that flags everything has
  perfect recall and is useless) - this set is the precision side. Each
  carries a predicate on the proposed target, deliberately loose: the exact
  attrib key is the reviewer's call, the concept is not.
- everything else is a **judgment call** (MEMBER_RATING has no listmonk
  equivalent; the unsubscribed scope and nonsubscribed semantics are real
  reviewer decisions; segments inherit flagged fields). Either disposition is
  defensible, so these are reported advisory and never gate.

Keys are `(kind, name)` with kind one of field / segment / consent.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

Key = tuple[str, str]


@dataclass(frozen=True)
class CaseExpectations:
    must_flag: frozenset[Key]
    must_propose: frozenset[Key]
    #: Target predicates for must_propose items, keyed like the sets. Loose on
    #: purpose: they pin the concept, not the reviewer's attrib spelling.
    targets: dict[Key, Callable[[str], bool]] = field(default_factory=dict)


def _contains(*needles: str) -> Callable[[str], bool]:
    return lambda t: any(n in t.lower() for n in needles)


_COMMON_TARGETS: dict[Key, Callable[[str], bool]] = {
    ("field", "EMAIL"): lambda t: t.lower() == "email",
    ("field", "FNAME"): _contains("first_name"),
    ("field", "LNAME"): _contains("last_name"),
    ("field", "STATE"): _contains("state"),
    ("field", "OPTIN_TIME"): _contains("optin", "opt_in"),
    ("field", "CONFIRM_TIME"): _contains("confirm"),
    # The draft's reviewed answer: enabled subscriber, confirmed subscription
    # (importing subscribed contacts unconfirmed would re-trigger opt-in mail).
    ("consent", "subscribed"): _contains("enabled"),
}

_COMMON_PROPOSE: frozenset[Key] = frozenset(_COMMON_TARGETS)

#: The real fixture payload: the three seeded ambiguities must come back
#: flagged, the unambiguous mappings must not.
FIXTURE = CaseExpectations(
    must_flag=frozenset(
        {("field", "ZIP"), ("field", "LASTPURCH"), ("segment", "VIP (legacy)")}
    ),
    must_propose=_COMMON_PROPOSE,
    targets=dict(_COMMON_TARGETS),
)

#: The clean variant: same account with the seeded ambiguities removed
#: (uniform 5-digit ZIPs, uniform ISO dates, no overlapping segment). ZIP and
#: LASTPURCH now have one defensible answer, so flagging them here is the
#: precision failure the fixture case cannot see.
CLEAN = CaseExpectations(
    must_flag=frozenset(),
    must_propose=_COMMON_PROPOSE
    | frozenset({("field", "ZIP"), ("field", "LASTPURCH")}),
    targets={
        **_COMMON_TARGETS,
        ("field", "ZIP"): _contains("zip"),
        ("field", "LASTPURCH"): _contains("purch"),
    },
)

#: The RC1-572 custom-fields corpus: the generator's default ZIP and
#: LASTPURCH seedings plus a new ambiguity class - a CUSTOM merge field with
#: mixed formats. LOYALTY_TIER mixes casings, trailing padding and numeric
#: tiers ('gold', 'GOLD ', '2', 'tier-3'): whether those are one tier or four
#: is the customer's call, so proposing a normalization is a guess. POINTS is
#: uniformly integer - its precision twin, one defensible answer.
CUSTOM_FIELDS = CaseExpectations(
    must_flag=frozenset(
        {("field", "ZIP"), ("field", "LASTPURCH"), ("field", "LOYALTY_TIER")}
    ),
    must_propose=_COMMON_PROPOSE | frozenset({("field", "POINTS")}),
    targets={**_COMMON_TARGETS, ("field", "POINTS"): _contains("point")},
)

BY_CASE: dict[str, CaseExpectations] = {
    "fixture-ambiguities": FIXTURE,
    "clean-variant": CLEAN,
    "custom-fields": CUSTOM_FIELDS,
}
