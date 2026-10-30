import type { AuditRecord } from '../api'
import './AuditPanel.css'

interface AuditPanelProps {
  records: AuditRecord[]
}

const EVENT_LABELS: Record<string, string> = {
  sign_in: 'signed in',
  sign_out: 'signed out',
  sign_in_refused: 'sign-in refused',
  access_revoked: 'access revoked by the IdP',
  mapping_resolved: 'resolved a mapping',
  approval_refused: 'approval refused by the gate',
  spec_approved: 'approved the spec',
  service_start: 'restarted the service; the spec reset to its committed draft',
}

function actorName(record: AuditRecord): string {
  const { actor } = record
  if (record.event === 'service_start') return 'system'
  if (actor.method === 'anonymous') return 'anonymous'
  if (actor.method === 'password') return 'demo approver (unverified)'
  return actor.email ?? actor.name ?? 'unknown'
}

function summary(record: AuditRecord): string {
  const d = record.detail
  if (record.event === 'mapping_resolved')
    return `${d.kind} ${String(d.source)} → ${String(d.status)}`
  if (record.event === 'spec_approved') return `sha256 ${String(d.content_sha256).slice(0, 12)}…`
  if (record.event === 'service_start') return `spec status: ${String(d.spec_status)}`
  if (typeof d.reason === 'string') return d.reason
  return ''
}

// The gate's decision log (RC1-554): newest first, each record hash-chained
// to the one before it on the server.
export default function AuditPanel({ records }: AuditPanelProps) {
  if (records.length === 0) {
    return <p className="audit-empty">No gate decisions recorded yet.</p>
  }
  return (
    <ol className="audit">
      {records.map((r) => (
        <li key={r.seq} className="audit-row">
          <span className="audit-at">{r.at.replace('T', ' ').replace('Z', ' UTC')}</span>
          <span>
            <strong>{actorName(r)}</strong> {EVENT_LABELS[r.event] ?? r.event}
          </span>
          <span className="audit-detail">{summary(r)}</span>
        </li>
      ))}
    </ol>
  )
}
