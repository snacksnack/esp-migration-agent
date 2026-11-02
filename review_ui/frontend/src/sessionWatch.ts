import { useEffect } from 'react'

// How often an open tab re-asks /api/me while signed in through the IdP
// (RC1-575). A SCIM deactivation ends the session server-side at once; this
// is how the page finds out without waiting for the user to click anything.
export const SESSION_WATCH_MS = 15_000

// Re-checks only while the tab is visible, and once more on returning to
// it, so a background tab costs nothing.
export function useSessionWatch(active: boolean, check: () => void): void {
  useEffect(() => {
    if (!active) return
    const tick = () => {
      if (document.visibilityState === 'visible') check()
    }
    const timer = window.setInterval(tick, SESSION_WATCH_MS)
    document.addEventListener('visibilitychange', tick)
    return () => {
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', tick)
    }
  }, [active, check])
}
