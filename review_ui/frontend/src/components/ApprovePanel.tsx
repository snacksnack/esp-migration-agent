import { useState } from 'react'
import { ApiError, type ApproveResult } from '../api'
import { useAuth } from '../authContext'
import { identityLabel } from '../identity'
import GateRefusal from './GateRefusal'
import SignInDialog from './SignInDialog'
import './ApprovePanel.css'

interface ApprovePanelProps {
  openQuestions: number
  onApprove: (approvedBy?: string) => Promise<ApproveResult>
  onLogin: (password: string) => Promise<unknown>
}

// The gate's front door. Approval needs a named owner; a refusal from
// mapping.gate.approve (open questions remain) renders verbatim - the UI
// never softens or second-guesses the gate, and anyone may trip it (RC1-562).
// Only a sign-off the gate would accept asks for the approver password: the
// backend answers 401 at that point, and the sign-in dialog retries.
// Under a verified SSO identity (RC1-554) there is no name to type: the IdP's
// identity is the name, and the receipt says how it was verified.
export default function ApprovePanel({ openQuestions, onApprove, onLogin }: ApprovePanelProps) {
  const { identity } = useAuth()
  const verified = identity?.verified === true
  const [owner, setOwner] = useState('')
  const [dialogMessage, setDialogMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<ApproveResult | null>(null)
  const [busy, setBusy] = useState(false)

  const approve = async () => {
    setBusy(true)
    setError(null)
    try {
      setResult(await onApprove(verified ? undefined : owner.trim()))
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        setDialogMessage(e.message) // "every gate passes - but signing off needs the approver sign-in"
      } else {
        setError(e instanceof Error ? e.message : String(e))
      }
    } finally {
      setBusy(false)
    }
  }

  if (result) {
    return (
      <div className="ap ap-done">
        <p>
          Approved by <strong>{result.approved_by}</strong>. Wrote <code>{result.out}</code>
        </p>
        {result.identity && <p className="ap-identity">{identityLabel(result.identity)}</p>}
        <p className="ap-sha">
          content sha256 <code>{result.content_sha256}</code>
        </p>
      </div>
    )
  }

  return (
    <div className="ap">
      {verified ? (
        <p className="ap-owner">
          Signing as <strong>{identity?.name ?? identity?.email}</strong>, {identityLabel(identity)}
        </p>
      ) : (
        <label className="ap-owner">
          Approving owner
          <input
            value={owner}
            onChange={(e) => setOwner(e.target.value)}
            placeholder="name recorded in the approval"
          />
        </label>
      )}
      <button
        className="ap-button"
        disabled={busy || (!verified && owner.trim() === '')}
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
      {error && <GateRefusal detail={error} />}
      {dialogMessage && (
        <SignInDialog
          message={dialogMessage}
          onLogin={onLogin}
          onSignedIn={() => {
            setDialogMessage(null)
            void approve()
          }}
          onClose={() => setDialogMessage(null)}
        />
      )}
    </div>
  )
}
