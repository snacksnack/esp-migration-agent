import type { MappingStatus } from '../api'
import './StatusBadge.css'

interface StatusBadgeProps {
  status: MappingStatus
}

// Glyph + text label carry the meaning; color reinforces it (RC1-559 idiom).
const GLYPHS: Record<MappingStatus, string> = {
  flagged: '▲',
  proposed: '○',
  approved: '●',
  edited: '●',
}

export default function StatusBadge({ status }: StatusBadgeProps) {
  return (
    <span className={`sb sb-${status}`}>
      <span aria-hidden="true" className="sb-glyph">
        {GLYPHS[status]}
      </span>
      {status}
    </span>
  )
}
