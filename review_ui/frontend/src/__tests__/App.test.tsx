import { render, screen } from '@testing-library/react'
import App from '../App'

// Route-aware fetch stub: only /api/me resolves; data fetches hang, which is
// fine - these tests cover the gate branch, not the review surface.
const stubMe = (me: { authenticated: boolean; auth_enabled: boolean }) =>
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      if (String(input) === '/api/me') {
        return Promise.resolve(new Response(JSON.stringify(me), { status: 200 }))
      }
      return new Promise<Response>(() => {})
    }),
  )

afterEach(() => vi.unstubAllGlobals())

describe('App auth gate', () => {
  it('shows the login screen when the gate is on and the viewer is not authenticated', async () => {
    stubMe({ authenticated: false, auth_enabled: true })
    render(<App />)
    expect(await screen.findByLabelText('Password')).toBeInTheDocument()
  })

  it('skips the login screen entirely when the gate is off', async () => {
    stubMe({ authenticated: true, auth_enabled: false })
    render(<App />)
    expect(await screen.findByText('Loading…')).toBeInTheDocument()
    expect(screen.queryByLabelText('Password')).not.toBeInTheDocument()
  })
})
