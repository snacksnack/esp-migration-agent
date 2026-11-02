import { render, renderHook, screen } from '@testing-library/react'
import type { AuditRecord } from '../api'
import AuditPanel from '../components/AuditPanel'
import { SESSION_WATCH_MS, useSessionWatch } from '../sessionWatch'

function setVisibility(state: DocumentVisibilityState) {
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: state })
  document.dispatchEvent(new Event('visibilitychange'))
}

describe('session watch (RC1-575)', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => {
    vi.useRealTimers()
    setVisibility('visible')
  })

  it('re-checks a signed-in SSO session while the tab is visible', () => {
    const check = vi.fn()
    renderHook(() => useSessionWatch(true, check))
    vi.advanceTimersByTime(SESSION_WATCH_MS * 2)
    expect(check).toHaveBeenCalledTimes(2)
  })

  it('stays quiet in a background tab and checks on return', () => {
    const check = vi.fn()
    renderHook(() => useSessionWatch(true, check))
    setVisibility('hidden')
    vi.advanceTimersByTime(SESSION_WATCH_MS * 3)
    expect(check).not.toHaveBeenCalled()
    setVisibility('visible')
    expect(check).toHaveBeenCalledTimes(1)
  })

  it('does nothing when nobody is signed in through the IdP', () => {
    const check = vi.fn()
    renderHook(() => useSessionWatch(false, check))
    vi.advanceTimersByTime(SESSION_WATCH_MS * 3)
    expect(check).not.toHaveBeenCalled()
  })
})

describe('SCIM events in the audit log', () => {
  it('names the IdP as the actor and counts the sessions it ended', () => {
    const record: AuditRecord = {
      seq: 7,
      at: '2026-11-02T02:00:00Z',
      event: 'scim_deactivated',
      actor: { method: 'anonymous', verified: false },
      detail: { provider: 'Okta SCIM', user_name: 'dana@customer.example', sessions_ended: 1 },
      sha256: 'b'.repeat(64),
    }
    render(<AuditPanel records={[record]} />)
    expect(screen.getByText('Okta SCIM')).toBeInTheDocument()
    expect(screen.getByText('deactivated a user')).toBeInTheDocument()
    expect(screen.getByText('dana@customer.example: 1 session ended')).toBeInTheDocument()
  })
})
