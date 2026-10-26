# Reconciliation report

Overall: **PASS**

## Consent counts

| State | Expected | In target | OK |
| --- | ---: | ---: | --- |
| blocklisted/unsubscribed | 411 | 411 | yes |
| enabled/confirmed | 3,756 | 3,756 | yes |
| enabled/unconfirmed | 88 | 88 | yes |
| total | 4,255 | 4,255 | yes |

## Suppression integrity (required: 100%)

411 of 411 suppressed addresses are non-subscribable in the target (100.0%).

## Segment parity

| Segment | Target | Plan | Source (ref) | OK |
| --- | ---: | ---: | ---: | --- |
| Brooklyn subscribers | 4 | 4 | 4 | yes |
| Highly engaged (4-5 stars) | 0 | 0 | 0 | yes |
| VIP customers | 1,709 | 1709 | 1,709 | yes |
| VIP (legacy) | 1,712 | 1712 | 1,712 | yes |

Source counts are references, not requirements: the approved spec may deliberately change segment semantics; parity is required between the plan and the target.

## Sampled checksums

51 of 51 sampled subscribers match field-for-field.

## Campaign translation

| Campaign | Residual source tags | Render comparison |
| --- | --- | --- |
| October newsletter | none | match |
| Brooklyn free delivery | none | match |
| VIP re-engagement | none | match |
