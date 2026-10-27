import type { Report } from '../api'
import BarChart from './BarChart'
import './ProfileCharts.css'

interface ProfileChartsProps {
  report: Report
}

const TENURE_LABELS: Record<string, string> = {
  lt_1y: 'Under 1 year',
  '1_3y': '1–3 years',
  '3_7y': '3–7 years',
  gt_7y: 'Over 7 years',
  unknown: 'Unknown',
}
const TENURE_ORDER = ['lt_1y', '1_3y', '3_7y', 'gt_7y', 'unknown']

const rows = (record: Record<string, number>): [string, number][] => Object.entries(record)

// Discovery context during review: the same audience-profile breakdowns the
// RC1-559 report renders, fed by the DiscoveryReport JSON endpoint. Context
// only - nothing here feeds the gate.
export default function ProfileCharts({ report }: ProfileChartsProps) {
  const tenure: [string, number][] = TENURE_ORDER.filter(
    (k) => k in report.profile.tenure_cohorts,
  ).map((k) => [TENURE_LABELS[k], report.profile.tenure_cohorts[k]])

  return (
    <section className="pc">
      <p className="pc-meta">
        {report.inventory.members_total.toLocaleString('en-US')} members ·{' '}
        {report.inventory.suppression_count.toLocaleString('en-US')} suppressions (migrate first) ·{' '}
        {report.risks.length} risks on the register
      </p>
      <div className="pc-grid">
        <BarChart title="Consent mix" rows={rows(report.profile.consent)} />
        <BarChart title="Top email domains" rows={rows(report.profile.top_domains)} />
        <BarChart title="Geography (top 10 states)" rows={rows(report.profile.states).slice(0, 10)} />
        <BarChart title="Signup tenure" rows={tenure} />
      </div>
    </section>
  )
}
