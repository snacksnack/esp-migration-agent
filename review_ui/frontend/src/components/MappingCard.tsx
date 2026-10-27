import { useState } from 'react'
import type { MappingView, Resolution } from '../api'
import ResolveForm from './ResolveForm'
import StatusBadge from './StatusBadge'
import './MappingCard.css'

interface MappingCardProps {
  mapping: MappingView
  onResolve: (mapping: MappingView, resolution: Resolution) => Promise<void>
  onLogin: (password: string) => Promise<unknown>
}

// One reviewed mapping: identity, confidence, rationale, and - when flagged -
// the open question front and center. Review opens the resolution form.
export default function MappingCard({ mapping, onResolve, onLogin }: MappingCardProps) {
  const [open, setOpen] = useState(false)
  const resolved = mapping.status === 'approved' || mapping.status === 'edited'
  const pct = Math.round(mapping.confidence * 100)

  return (
    <article className={`mc${mapping.status === 'flagged' ? ' mc-flagged' : ''}`}>
      <header className="mc-head">
        <span className="mc-source">{mapping.source}</span>
        {mapping.context && <span className="mc-context">{mapping.context}</span>}
        <span className="mc-spacer" />
        <StatusBadge status={mapping.status} />
      </header>
      <div className="mc-confidence" title={`proposer confidence ${pct}%`}>
        <span className="mc-confidence-track">
          <span className="mc-confidence-fill" style={{ width: `${pct}%` }} />
        </span>
        <span className="mc-confidence-val">{pct}%</span>
      </div>
      <p className="mc-rationale">{mapping.rationale}</p>
      {mapping.samples.length > 0 && (
        <p className="mc-samples">Samples: {mapping.samples.join(' · ')}</p>
      )}
      {mapping.openQuestion && <p className="mc-question">{mapping.openQuestion}</p>}
      {resolved && mapping.target && (
        <p className="mc-target">
          → <code>{mapping.target}</code>
        </p>
      )}
      {!resolved && (
        <button className="mc-toggle" onClick={() => setOpen(!open)}>
          {open ? 'Close' : 'Review'}
        </button>
      )}
      {!resolved && open && (
        <ResolveForm
          mapping={mapping}
          onResolve={(resolution) => onResolve(mapping, resolution)}
          onLogin={onLogin}
        />
      )}
    </article>
  )
}
