import { useState } from 'react'
import { SSO_START_URL } from '../api'
import { useAuth } from '../authContext'
import './SignInDialog.css'

interface SignInDialogProps {
  message: string // the backend's 401 detail, shown verbatim
  onLogin: (password: string) => Promise<unknown>
  onSignedIn: () => void // retries the action that hit the 401
  onClose: () => void
}

// The demo-mode popup (RC1-562): an anonymous write answers 401 with an
// explanation, and this dialog shows it - browsing stays open, changing the
// spec is the approver's. With SSO configured (RC1-554) the primary path is
// the customer's IdP, which leaves the page and comes back signed in; the
// shared password stays as the fallback and is labeled for what it is.
export default function SignInDialog({ message, onLogin, onSignedIn, onClose }: SignInDialogProps) {
  const { ssoProvider, passwordEnabled, refresh } = useAuth()
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const signIn = async () => {
    setBusy(true)
    setError(null)
    try {
      await onLogin(password)
      refresh()
      onSignedIn()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="sd-overlay" role="dialog" aria-modal="true" aria-label="Approver sign-in">
      <div className="sd-card">
        <h2>Demo mode</h2>
        <p className="sd-message">{message}</p>
        <p className="sd-blurb">
          In a real engagement this is the point of the system: anyone can review the migration, but
          only the named owner signs it off.
        </p>
        {ssoProvider && (
          <div className="sd-sso">
            <a className="sd-sso-button" href={SSO_START_URL}>
              Sign in with {ssoProvider}
            </a>
            <p className="sd-blurb">
              The customer's identity provider vouches for you: approvals carry that verified
              identity, and deactivating you there ends your access here.
            </p>
          </div>
        )}
        {passwordEnabled && (
          <label className="sd-field">
            {ssoProvider ? 'Or the demo password (recorded as unverified)' : 'Approver password'}
            <input
              type="password"
              autoFocus={!ssoProvider}
              value={password}
              onChange={(e) => {
                setPassword(e.target.value)
                setError(null)
              }}
              placeholder="Password"
            />
          </label>
        )}
        {error && (
          <p className="sd-error" role="alert">
            {error}
          </p>
        )}
        <div className="sd-actions">
          {passwordEnabled && (
            <button
              className={ssoProvider ? undefined : 'sd-primary'}
              disabled={busy || !password}
              onClick={() => void signIn()}
            >
              Sign in and continue
            </button>
          )}
          <button onClick={onClose}>Keep browsing</button>
        </div>
      </div>
    </div>
  )
}
