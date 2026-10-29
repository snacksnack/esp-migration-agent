import { createContext, useContext } from 'react'
import type { ApproverIdentity } from './api'

// Sign-in options and the current identity (RC1-554), shared so the sign-in
// dialog and approval panel need no prop threading through every card. The
// default is the pre-SSO demo: password only, nobody signed in.
export interface AuthState {
  ssoProvider: string | null
  passwordEnabled: boolean
  identity: ApproverIdentity | null
  refresh: () => void
}

export const AuthContext = createContext<AuthState>({
  ssoProvider: null,
  passwordEnabled: true,
  identity: null,
  refresh: () => {},
})

export const useAuth = () => useContext(AuthContext)
