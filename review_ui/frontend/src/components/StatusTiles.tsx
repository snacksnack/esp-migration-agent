import type { MappingView } from '../api'
import './StatusTiles.css'

interface StatusTilesProps {
  mappings: MappingView[]
}

// The reviewer's worklist at a glance: mapping.review's render_status counts
// as tiles. Open questions is the number the approve gate cares about.
export default function StatusTiles({ mappings }: StatusTilesProps) {
  const count = (status: string) => mappings.filter((m) => m.status === status).length
  const tiles = [
    { label: 'Mappings', value: mappings.length },
    { label: 'Awaiting review', value: count('proposed') },
    { label: 'Open questions', value: count('flagged') },
    { label: 'Resolved', value: count('approved') + count('edited') },
  ]
  return (
    <div className="st">
      {tiles.map((t) => (
        <div className="st-tile" key={t.label}>
          <div className="st-label">{t.label}</div>
          <div className="st-value">{t.value}</div>
        </div>
      ))}
    </div>
  )
}
