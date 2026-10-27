import { useState } from 'react'
import type { MappingView, Resolution } from '../api'
import './ResolveForm.css'

interface ResolveFormProps {
  mapping: MappingView
  onResolve: (resolution: Resolution) => Promise<void>
}

const TARGET_LABELS = {
  field: 'Target field',
  segment: 'Target query',
  consent: 'Target status',
}

// The resolution form mirrors what a hand edit of the YAML does: pick
// approved (as proposed) or edited, fill the target, and for field mappings
// record the decision in the transform. The backend re-validates through the
// spec schema, so a flagged mapping cannot leave here without a target.
export default function ResolveForm({ mapping, onResolve }: ResolveFormProps) {
  const [target, setTarget] = useState(mapping.target ?? '')
  const [transform, setTransform] = useState(mapping.transform ?? '')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const submit = async (resolution: Resolution) => {
    setBusy(true)
    setError(null)
    try {
      await onResolve(resolution)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const saveEdit = () => {
    const resolution: Resolution = { status: 'edited', target: target.trim() }
    if (mapping.kind === 'field') resolution.transform = transform.trim()
    void submit(resolution)
  }

  return (
    <div className="rf">
      <label className="rf-row">
        <span className="rf-label">{TARGET_LABELS[mapping.kind]}</span>
        <input value={target} onChange={(e) => setTarget(e.target.value)} />
      </label>
      {mapping.kind === 'field' && (
        <label className="rf-row">
          <span className="rf-label">Transform / decision</span>
          <textarea rows={2} value={transform} onChange={(e) => setTransform(e.target.value)} />
        </label>
      )}
      <div className="rf-actions">
        {mapping.status === 'proposed' && (
          <button disabled={busy} onClick={() => void submit({ status: 'approved' })}>
            Accept as proposed
          </button>
        )}
        <button className="rf-primary" disabled={busy || target.trim() === ''} onClick={saveEdit}>
          Save as edited
        </button>
      </div>
      {error && (
        <p className="rf-error" role="alert">
          {error}
        </p>
      )}
    </div>
  )
}
