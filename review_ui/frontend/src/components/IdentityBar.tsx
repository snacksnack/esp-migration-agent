import { SSO_START_URL } from '../api'
import { useAuth } from '../authContext'
import { identityLabel } from '../identity'
import './IdentityBar.css'

interface IdentityBarProps {
  onLogout: () => Promise<unknown>
}

// Who is signed in, and how we know (RC1-554). Absent when the gate is off.
export default function IdentityBar({ onLogout }: IdentityBarProps) {
  const { identity, ssoProvider, refresh } = useAuth()

  if (!identity) {
    return (
      <div className="ib">
        <span className="ib-who">Browsing anonymously; reading is open, changes need sign-in.</span>
        {ssoProvider && (
          <a className="ib-action" href={SSO_START_URL}>
            Sign in with {ssoProvider}
          </a>
        )}
      </div>
    )
  }
  return (
    <div className="ib">
      <span className="ib-who">
        Signed in as <strong>{identity.name ?? identity.email ?? 'demo approver'}</strong>
        <span className={identity.verified ? 'ib-verified' : 'ib-unverified'}>
          {identityLabel(identity)}
        </span>
      </span>
      <button className="ib-action" onClick={() => void onLogout().then(refresh)}>
        Sign out
      </button>
    </div>
  )
}
