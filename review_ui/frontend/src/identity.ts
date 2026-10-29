import type { ApproverIdentity } from './api'

// One phrasing of "how do we know who this is" (RC1-554), shared by the
// identity bar, the approval receipt, and the audit panel.
export function identityLabel(identity: ApproverIdentity | null | undefined): string | null {
  if (!identity) return null
  if (identity.verified) {
    const who = identity.email ?? identity.subject
    return `verified by ${identity.issuer} (${who})`
  }
  return 'unverified: shared demo password, name as typed'
}
