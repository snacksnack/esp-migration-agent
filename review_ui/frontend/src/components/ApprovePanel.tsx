import { useState } from 'react'
import { ApiError, type ApproveResult } from '../api'
import './ApprovePanel.css'

interface ApprovePanelProps {
  openQuestions: number
  onApprove: (approvedBy: string) => Promise<ApproveResult>
  onLogin: (password: string) => Promise<unknown>
}

// The gate's front door. Approval needs a named owner; a refusal from
// mapping.gate.approve (open questions remain) renders verbatim - the UI
// never softens or second-guesses the gate, and anyone may trip it (RC1-562).
// Only a sign-off the gate would accept asks for the approver password: the
// backend answers 401 at that point, and the password step appears here.
export default function ApprovePanel({ openQuestions, onApprove, onLogin }: ApprovePanelProps) {
  const [owner, setOwner] = useState('')
  const [password, setPassword] = useState('')
  const [needsLogin, setNeedsLogin] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<ApproveResult | null>(null)
  const [busy, setBusy] = useState(false)

  const approve = async () => {
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      setResult(await onApprove(owner.trim()))
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        setNeedsLogin(true)
        setNotice(e.message) // "every gate passes - but signing off needs the approver login"
      } else {
        setError(e instanceof Error ? e.message : String(e))
      }
    } finally {
      setBusy(false)
    }
  }

  const loginAndApprove = async () => {
    setBusy(true)
    setError(null)
    try {
      await onLogin(password)
      setResult(await onApprove(owner.trim()))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  if (result) {
    return (
      <div className="ap ap-done">
        <p>
          Approved by <strong>{result.approved_by}</strong>. Wrote{' '}
          <code>{result.out}</code>
        </p>
        <p className="ap-sha">
          content sha256 <code>{result.content_sha256}</code>
        </p>
      </div>
    )
  }

  return (
    <div className="ap">
      <label className="ap-owner">
        Approving owner
        <input
          value={owner}
          onChange={(e) => setOwner(e.target.value)}
          placeholder="name recorded in the approval"
        />
      </label>
      <button
        className="ap-button"
        disabled={busy || owner.trim() === ''}
        onClick={() => void approve()}
      >
        Approve spec
      </button>
      {openQuestions > 0 && (
        <p className="ap-hint">
          {openQuestions} open question{openQuestions === 1 ? '' : 's'} remain — the gate will
          refuse until every mapping is resolved.
        </p>
      )}
      {notice && <p className="ap-hint">{notice}</p>}
      {needsLogin && (
        <div className="ap-login">
          <label className="ap-owner">
            Approver password
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="Password"
            />
          </label>
          <button
            className="ap-button"
            disabled={busy || password === '' || owner.trim() === ''}
            onClick={() => void loginAndApprove()}
          >
            Sign in and approve
          </button>
        </div>
      )}
      {error && (
        <p className="ap-error" role="alert">
          {error}
        </p>
      )}
    </div>
  )
}
