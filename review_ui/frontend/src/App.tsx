import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  approveSpec,
  getAudit,
  getMe,
  getReport,
  getSpec,
  login,
  logout,
  mappingViews,
  resolveMapping,
  type AuditRecord,
  type MappingView,
  type Me,
  type Report,
  type Resolution,
  type SpecResponse,
} from './api'
import { AuthContext, type AuthState } from './authContext'
import ApprovePanel from './components/ApprovePanel'
import AuditPanel from './components/AuditPanel'
import IdentityBar from './components/IdentityBar'
import MappingCard from './components/MappingCard'
import ProfileCharts from './components/ProfileCharts'
import StatusTiles from './components/StatusTiles'
import './App.css'

const SECTIONS: { kind: MappingView['kind']; title: string }[] = [
  { kind: 'field', title: 'Field mappings' },
  { kind: 'segment', title: 'Segment mappings' },
  { kind: 'consent', title: 'Consent mappings' },
]

export default function App() {
  const [specResponse, setSpecResponse] = useState<SpecResponse | null>(null)
  const [report, setReport] = useState<Report | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [me, setMe] = useState<Me | null>(null)
  const [audit, setAudit] = useState<AuditRecord[]>([])
  // An IdP refusal comes back as /?sso_error=... (RC1-554); show it once.
  const [ssoError, setSsoError] = useState<string | null>(() =>
    new URLSearchParams(window.location.search).get('sso_error'),
  )

  const refreshAuth = useCallback(() => {
    getMe()
      .then(setMe)
      .catch(() => setMe(null))
    getAudit()
      .then((r) => setAudit(r.records))
      .catch(() => setAudit([]))
  }, [])

  useEffect(() => {
    refreshAuth()
    if (ssoError) window.history.replaceState(null, '', window.location.pathname)
  }, [refreshAuth, ssoError])

  const auth = useMemo<AuthState>(
    () => ({
      ssoProvider: me?.sso_provider ?? null,
      passwordEnabled: me?.password_enabled ?? true,
      identity: me?.identity ?? null,
      refresh: refreshAuth,
    }),
    [me, refreshAuth],
  )

  useEffect(() => {
    // Reading is public (RC1-562): the review surface loads for everyone.
    // The one gated act, a successful sign-off, is handled in ApprovePanel.
    getSpec()
      .then(setSpecResponse)
      .catch((e: Error) => setLoadError(e.message))
    getReport()
      .then(setReport)
      .catch(() => setReport(null)) // charts are context; review works without them
  }, [])

  if (loadError) {
    return (
      <main className="app">
        <p className="app-error" role="alert">
          {loadError}
        </p>
      </main>
    )
  }
  if (!specResponse) return <main className="app">Loading…</main>

  const { path, spec } = specResponse
  const mappings = mappingViews(spec)
  const openQuestions = mappings.filter((m) => m.status === 'flagged').length

  const onResolve = async (mapping: MappingView, resolution: Resolution) => {
    try {
      setSpecResponse(await resolveMapping(mapping, resolution))
    } finally {
      refreshAuth() // a revoked identity signs out server-side; the bar should say so
    }
  }

  const onApprove = async (approvedBy?: string) => {
    try {
      return await approveSpec(approvedBy)
    } finally {
      refreshAuth()
    }
  }

  return (
    <AuthContext.Provider value={auth}>
      <main className="app">
        {me?.auth_enabled && <IdentityBar onLogout={logout} />}
        {ssoError && (
          <p className="app-error" role="alert">
            {ssoError} <button onClick={() => setSsoError(null)}>Dismiss</button>
          </p>
        )}
        <header>
          <h1>
            Review gate: {spec.source} → {spec.target}
          </h1>
          <p className="app-subtitle">
            Document of record: <code>{path}</code> · spec status {spec.status}. Resolutions edit
            the YAML; approval goes through the gate and pins a content hash.
          </p>
        </header>

        <StatusTiles mappings={mappings} />

        {SECTIONS.map(({ kind, title }) => (
          <section key={kind}>
            <h2>{title}</h2>
            <div className="app-cards">
              {mappings
                .filter((m) => m.kind === kind)
                .map((m) => (
                  <MappingCard
                    key={`${kind}-${m.index}`}
                    mapping={m}
                    onResolve={onResolve}
                    onLogin={login}
                  />
                ))}
            </div>
          </section>
        ))}

        <section>
          <h2>Approval</h2>
          <ApprovePanel openQuestions={openQuestions} onApprove={onApprove} onLogin={login} />
        </section>

        {me?.auth_enabled && (
          <section>
            <h2>Audit log</h2>
            <AuditPanel records={audit} />
          </section>
        )}

        {report && (
          <section>
            <h2>Discovery context</h2>
            <ProfileCharts report={report} />
          </section>
        )}
      </main>
    </AuthContext.Provider>
  )
}
