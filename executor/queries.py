"""The executor's tiny segment-query dialect (RC1-546/547).

The approved spec's segment queries use a deliberately small SQL shape; this
module re-evaluates it in Python. Reconciliation uses it for plan-side parity
counts, the fake target uses it to answer count_query, and the Brevo adapter
uses it to count segments client-side (Brevo has no server-side query API).
A shape it does not know raises QueryNotEvaluatable - never a guess.
"""

from __future__ import annotations

import re

_LIKE_RE = re.compile(r"^subscribers\.attribs->>'(?P<key>\w+)' LIKE '(?P<pat>[^']*)'$")
_INT_CMP_RE = re.compile(
    r"^\(subscribers\.attribs->>'(?P<key>\w+)'\)::int (?P<op>>=|<=|>|<|=) (?P<n>-?\d+)$"
)


class QueryNotEvaluatable(RuntimeError):
    pass


def _like_to_predicate(pattern: str):
    regex = re.compile("^" + ".*".join(re.escape(p) for p in pattern.split("%")) + "$")
    return lambda value: bool(regex.match(value))


def evaluate_query(query: str, attribs: dict[str, str | int]) -> bool:
    """Evaluate one segment query clause-by-clause (top-level OR only)."""
    for clause in query.split(" OR "):
        clause = clause.strip()
        if m := _LIKE_RE.match(clause):
            value = str(attribs.get(m["key"], ""))
            if _like_to_predicate(m["pat"])(value):
                return True
        elif m := _INT_CMP_RE.match(clause):
            raw = attribs.get(m["key"])
            if raw is None or str(raw).lstrip("-") == "" or not str(raw).lstrip("-").isdigit():
                continue
            value, n = int(raw), int(m["n"])
            ops = {
                ">": value > n, ">=": value >= n, "<": value < n,
                "<=": value <= n, "=": value == n,
            }
            if ops[m["op"]]:
                return True
        else:
            raise QueryNotEvaluatable(clause)
    return False
