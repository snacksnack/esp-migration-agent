"""Self-contained HTML render of the discovery report (RC1-559).

One file, no server, no external assets: the artifact an FDE emails the
client after discovery. Every number comes from the same DiscoveryReport the
markdown render reads - this module draws, it never computes. Charts are
single-measure breakdowns, so each is one hue with values labelled at the
bar tips; risk severities use the reserved status colors with a glyph and a
text label, never color alone. Each chart carries a collapsible table of the
same numbers as the accessible fallback.
"""

from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # import cycle guard: report.py imports this module
    from discovery.report import DiscoveryReport

# Severity -> (status color, glyph). Reserved status palette, mode-invariant;
# the glyph + text label carry the meaning, the color reinforces it.
_SEVERITY_BADGE = {
    "high": ("#d03b3b", "▲"),  # critical red, triangle
    "medium": ("#ec835a", "◆"),  # serious orange, diamond
    "low": ("#fab219", "●"),  # warning yellow, circle
}

_TENURE_LABELS = {
    "lt_1y": "Under 1 year",
    "1_3y": "1–3 years",
    "3_7y": "3–7 years",
    "gt_7y": "Over 7 years",
    "unknown": "Unknown",
}
_TENURE_ORDER = ["lt_1y", "1_3y", "3_7y", "gt_7y", "unknown"]

_CSS = """
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb;
  --ink: #0b0b0b; --ink-2: #52514e; --ink-muted: #898781;
  --hairline: #e1e0d9; --baseline: #c3c2b7;
  --bar: #2a78d6;
  --border: rgba(11, 11, 11, 0.1);
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19;
    --ink: #f0efec; --ink-2: #c3c2b7; --ink-muted: #898781;
    --hairline: #2c2c2a; --baseline: #383835;
    --bar: #3987e5;
    --border: rgba(255, 255, 255, 0.1);
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--page); color: var(--ink);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
}
main { max-width: 880px; margin: 0 auto; padding: 32px 16px 64px; }
h1 { font-size: 1.5rem; margin: 0 0 4px; }
h2 { font-size: 1.05rem; margin: 40px 0 12px; }
.subtitle, figcaption, .note { color: var(--ink-2); font-size: 0.85rem; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 12px; margin-top: 24px; }
.tile { background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 12px 14px; }
.tile .label { color: var(--ink-2); font-size: 0.78rem; }
.tile .value { font-size: 1.5rem; font-weight: 600; }
figure { margin: 16px 0 0; background: var(--surface);
  border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
figure h3 { font-size: 0.9rem; margin: 0 0 12px; }
.row { display: grid; grid-template-columns: 150px 1fr; align-items: center;
  column-gap: 10px; margin: 6px 0; }
.row .cat { color: var(--ink-2); font-size: 0.8rem; text-align: right;
  overflow-wrap: anywhere; }
.row .track { display: flex; align-items: center; gap: 8px; min-width: 0; }
.row .fill { height: 18px; background: var(--bar);
  border-radius: 0 4px 4px 0; flex: none; }
.row .val { color: var(--ink-2); font-size: 0.8rem; white-space: nowrap;
  font-variant-numeric: tabular-nums; }
details { margin-top: 10px; }
details summary { color: var(--ink-muted); font-size: 0.78rem; cursor: pointer; }
table { border-collapse: collapse; width: 100%; margin-top: 8px;
  font-size: 0.85rem; }
th { text-align: left; color: var(--ink-2); font-weight: 600; }
th, td { padding: 6px 10px 6px 0; border-bottom: 1px solid var(--hairline); }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.sev { white-space: nowrap; }
.sev .glyph { font-size: 0.7rem; margin-right: 4px; }
ul { margin: 8px 0; padding-left: 20px; }
.prose { background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 16px; margin-top: 16px; }
@media (max-width: 520px) { .row { grid-template-columns: 96px 1fr; } }
"""


def _fmt(n: int | float) -> str:
    if isinstance(n, float) and not n.is_integer():
        return f"{n:,.1f}"
    return f"{int(n):,}"


def _bars(rows: list[tuple[str, int]], aria: str) -> str:
    """One single-hue bar per row, value at the tip, widths from the max."""
    peak = max((v for _, v in rows), default=0)
    out = [f'<div role="img" aria-label="{escape(aria)}">']
    for label, value in rows:
        pct = 0.0 if peak == 0 else (value / peak) * 100
        tip = f"{escape(label)}: {_fmt(value)}"
        out.append(
            '<div class="row">'
            f'<span class="cat">{escape(label)}</span>'
            '<span class="track">'
            f'<span class="fill" style="width:{pct:.1f}%" title="{tip}"></span>'
            f'<span class="val">{_fmt(value)}</span>'
            "</span></div>"
        )
    out.append("</div>")
    return "".join(out)


def _table(headers: tuple[str, str], rows: list[tuple[str, int]]) -> str:
    body = "".join(
        f"<tr><td>{escape(k)}</td><td class='num'>{_fmt(v)}</td></tr>" for k, v in rows
    )
    return (
        "<details><summary>Table view</summary><table>"
        f"<tr><th>{escape(headers[0])}</th><th class='num'>{escape(headers[1])}</th></tr>"
        f"{body}</table></details>"
    )


def _chart(title: str, rows: list[tuple[str, int]], headers: tuple[str, str]) -> str:
    return (
        f"<figure><h3>{escape(title)}</h3>"
        f"{_bars(rows, title)}{_table(headers, rows)}</figure>"
    )


def _severity_cell(severity: str) -> str:
    color, glyph = _SEVERITY_BADGE[severity]
    return (
        f'<span class="sev"><span class="glyph" style="color:{color}">{glyph}</span>'
        f"{escape(severity)}</span>"
    )


def _tile(label: str, value: str) -> str:
    return (
        f'<div class="tile"><div class="label">{escape(label)}</div>'
        f'<div class="value">{escape(value)}</div></div>'
    )


def render_html(report: DiscoveryReport) -> str:
    inv, prof = report.inventory, report.profile

    tiles = "".join(
        [
            _tile("Members in export", _fmt(inv.members_total)),
            _tile("Suppressions (migrate first)", _fmt(inv.suppression_count)),
            _tile("Segments", _fmt(len(inv.segments))),
            _tile("Templates", _fmt(len(inv.templates))),
            _tile("Risks on the register", _fmt(len(report.risks))),
            _tile("Estimated hours", _fmt(report.proposal.total_hours)),
        ]
    )

    tenure_rows = [
        (_TENURE_LABELS[k], prof.tenure_cohorts[k])
        for k in _TENURE_ORDER
        if k in prof.tenure_cohorts
    ]
    rating_rows = [
        (f"{k} star{'s' if k != 1 else ''}", v) for k, v in prof.member_rating.items()
    ]
    charts = "".join(
        [
            _chart("Consent mix", list(prof.consent.items()), ("Status", "Members")),
            _chart(
                "Consumer vs corporate domains",
                list(prof.domain_class.items()),
                ("Class", "Members"),
            ),
            _chart("Top email domains", list(prof.top_domains.items()), ("Domain", "Members")),
            _chart(
                "Geography (top 10 states)",
                list(prof.states.items())[:10],
                ("State", "Members"),
            ),
            _chart("Signup tenure", tenure_rows, ("Cohort", "Members")),
            _chart("Engagement (member rating)", rating_rows, ("Rating", "Members")),
        ]
    )

    risk_rows = "".join(
        "<tr>"
        f"<td>{_severity_cell(r.severity)}</td>"
        f"<td>{escape(r.title)}</td>"
        f"<td class='num'>{_fmt(r.count) if r.count is not None else '-'}</td>"
        f"<td>{escape(r.detail)}</td>"
        "</tr>"
        for r in report.risks
    )
    ws_rows = "".join(
        "<tr>"
        f"<td>{escape(w.name)}</td><td>{escape(w.driver)}</td>"
        f"<td class='num'>{_fmt(w.units)}</td><td class='num'>{_fmt(w.est_hours)}</td>"
        "</tr>"
        for w in report.proposal.workstreams
    )

    narration = (
        f"<h2>Who this audience is</h2><div class='prose'>{escape(report.narration)}</div>"
        if report.narration
        else ""
    )
    assumptions = "".join(f"<li>{escape(a)}</li>" for a in report.proposal.assumptions)
    out_of_scope = "".join(f"<li>{escape(o)}</li>" for o in report.proposal.out_of_scope)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Discovery report: {escape(report.source)} → {escape(report.target)}</title>
<style>{_CSS}</style>
</head>
<body>
<main>
<h1>Discovery report: {escape(report.source)} → {escape(report.target)}</h1>
<p class="subtitle">Audience data as of {report.as_of.date().isoformat()}. Every
count is computed deterministically from the export bundle; aggregates only, no
record-level data appears in this report.</p>
<div class="tiles">{tiles}</div>
{narration}
<h2>Audience profile</h2>
{charts}
<h2>Risk register</h2>
<figure><table>
<tr><th>Severity</th><th>Risk</th><th class="num">Affected</th><th>Detail</th></tr>
{risk_rows}
</table></figure>
<h2>Scope and estimate proposal</h2>
<figure><table>
<tr><th>Workstream</th><th>Driver</th><th class="num">Units</th><th class="num">Est. hours</th></tr>
{ws_rows}
<tr><td><strong>Total</strong></td><td></td><td></td>
<td class="num"><strong>{_fmt(report.proposal.total_hours)}</strong></td></tr>
</table>
<h3>Assumptions</h3><ul>{assumptions}</ul>
<h3>Out of scope</h3><ul>{out_of_scope}</ul>
</figure>
<h2>Send-ramp recommendation</h2>
<div class="prose">{escape(report.ramp_recommendation)}</div>
</main>
</body>
</html>
"""
