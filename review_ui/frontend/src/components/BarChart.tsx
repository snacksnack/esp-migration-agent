import './BarChart.css'

interface BarChartProps {
  title: string
  rows: [string, number][]
}

const fmt = (n: number) => n.toLocaleString('en-US')

// RC1-559's chart idiom in React: single-measure breakdown, one hue, value
// labelled at the bar tip, widths scaled to the max.
export default function BarChart({ title, rows }: BarChartProps) {
  const peak = Math.max(...rows.map(([, v]) => v), 0)
  return (
    <figure className="bc">
      <h3>{title}</h3>
      <div role="img" aria-label={title}>
        {rows.map(([label, value]) => (
          <div className="bc-row" key={label}>
            <span className="bc-cat">{label}</span>
            <span className="bc-track">
              <span
                className="bc-fill"
                style={{ width: `${peak === 0 ? 0 : (value / peak) * 100}%` }}
                title={`${label}: ${fmt(value)}`}
              />
              <span className="bc-val">{fmt(value)}</span>
            </span>
          </div>
        ))}
      </div>
    </figure>
  )
}
