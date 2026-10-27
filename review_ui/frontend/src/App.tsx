import { useEffect, useState } from 'react'
import {
  approveSpec,
  getMe,
  getReport,
  getSpec,
  mappingViews,
  resolveMapping,
  type MappingView,
  type Report,
  type Resolution,
  type SpecResponse,
} from './api'
import ApprovePanel from './components/ApprovePanel'
import Login from './components/Login'
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
  const [authState, setAuthState] = useState<'checking' | 'login' | 'ok'>('checking')
  const [specResponse, setSpecResponse] = useState<SpecResponse | null>(null)
  const [report, setReport] = useState<Report | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)

  const loadData = () => {
    getSpec()
      .then(setSpecResponse)
      .catch((e: Error) => setLoadError(e.message))
    getReport()
      .then(setReport)
      .catch(() => setReport(null)) // charts are context; review works without them
  }

  useEffect(() => {
    // The gate (RC1-561): the auth probe decides between the login screen and
    // the review surface. With the gate off the probe answers authenticated.
    getMe()
      .then((me) => {
        if (me.auth_enabled && !me.authenticated) {
          setAuthState('login')
        } else {
          setAuthState('ok')
          loadData()
        }
      })
      .catch((e: Error) => setLoadError(e.message))
  }, [])

  if (authState === 'login') {
    return (
      <Login
        onSuccess={() => {
          setAuthState('ok')
          loadData()
        }}
      />
    )
  }
  if (loadError) {
    return (
      <main className="app">
        <p className="app-error" role="alert">
          {loadError}
        </p>
      </main>
    )
  }
  if (authState === 'checking' || !specResponse) return <main className="app">Loading…</main>

  const { path, spec } = specResponse
  const mappings = mappingViews(spec)
  const openQuestions = mappings.filter((m) => m.status === 'flagged').length

  const onResolve = async (mapping: MappingView, resolution: Resolution) => {
    setSpecResponse(await resolveMapping(mapping, resolution))
  }

  return (
    <main className="app">
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
                <MappingCard key={`${kind}-${m.index}`} mapping={m} onResolve={onResolve} />
              ))}
          </div>
        </section>
      ))}

      <section>
        <h2>Approval</h2>
        <ApprovePanel openQuestions={openQuestions} onApprove={approveSpec} />
      </section>

      {report && (
        <section>
          <h2>Discovery context</h2>
          <ProfileCharts report={report} />
        </section>
      )}
    </main>
  )
}
