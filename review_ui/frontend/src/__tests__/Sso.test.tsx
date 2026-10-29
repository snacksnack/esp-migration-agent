import { fireEvent, render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { ApiError, type ApproverIdentity, type AuditRecord } from '../api'
import { AuthContext, type AuthState } from '../authContext'
import ApprovePanel from '../components/ApprovePanel'
import AuditPanel from '../components/AuditPanel'
import IdentityBar from '../components/IdentityBar'
import SignInDialog from '../components/SignInDialog'

const DANA: ApproverIdentity = {
  method: 'oidc',
  verified: true,
  issuer: 'https://customer.okta.com',
  subject: '00u-dana',
  email: 'dana@customer.example',
  name: 'Dana Ops',
}

function withAuth(ui: ReactNode, overrides: Partial<AuthState> = {}) {
  const value: AuthState = {
    ssoProvider: 'Okta',
    passwordEnabled: true,
    identity: null,
    refresh: vi.fn(),
    ...overrides,
  }
  return render(<AuthContext.Provider value={value}>{ui}</AuthContext.Provider>)
}

describe('SSO sign-in (RC1-554)', () => {
  it('offers the IdP first and labels the password as unverified', () => {
    withAuth(
      <SignInDialog
        message="needs sign-in"
        onLogin={vi.fn()}
        onSignedIn={vi.fn()}
        onClose={vi.fn()}
      />,
    )
    expect(screen.getByRole('link', { name: 'Sign in with Okta' })).toHaveAttribute(
      'href',
      '/api/auth/sso/start',
    )
    expect(
      screen.getByLabelText('Or the demo password (recorded as unverified)'),
    ).toBeInTheDocument()
  })

  it('SSO-only deploys show no password field', () => {
    withAuth(
      <SignInDialog
        message="needs sign-in"
        onLogin={vi.fn()}
        onSignedIn={vi.fn()}
        onClose={vi.fn()}
      />,
      { passwordEnabled: false },
    )
    expect(screen.queryByLabelText(/password/i)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Sign in and continue' })).not.toBeInTheDocument()
  })

  it('a verified approver signs without typing a name', async () => {
    const onApprove = vi.fn().mockResolvedValue({
      out: 'specs/approved/x.yaml',
      approved_by: 'Dana Ops',
      approved_at: '2026-10-29T12:00:00+00:00',
      content_sha256: 'b'.repeat(64),
      identity: DANA,
    })
    withAuth(<ApprovePanel openQuestions={0} onApprove={onApprove} onLogin={vi.fn()} />, {
      identity: DANA,
    })
    expect(screen.queryByLabelText('Approving owner')).not.toBeInTheDocument()
    expect(screen.getByText(/Signing as/)).toHaveTextContent('Dana Ops')
    fireEvent.click(screen.getByRole('button', { name: 'Approve spec' }))
    expect(
      await screen.findByText('verified by https://customer.okta.com (dana@customer.example)'),
    ).toBeInTheDocument()
    expect(onApprove).toHaveBeenCalledWith(undefined)
  })

  it('a revoked identity surfaces the 401 with the IdP named', async () => {
    const onApprove = vi
      .fn()
      .mockRejectedValue(new ApiError(401, 'Okta no longer vouches for your sign-in'))
    withAuth(<ApprovePanel openQuestions={0} onApprove={onApprove} onLogin={vi.fn()} />, {
      identity: DANA,
    })
    fireEvent.click(screen.getByRole('button', { name: 'Approve spec' }))
    expect(await screen.findByRole('dialog')).toHaveTextContent('Okta no longer vouches')
  })

  it('identity bar distinguishes verified from password sessions', () => {
    const { unmount } = withAuth(<IdentityBar onLogout={vi.fn()} />, { identity: DANA })
    expect(screen.getByText(/verified by https:\/\/customer.okta.com/)).toBeInTheDocument()
    unmount()
    withAuth(<IdentityBar onLogout={vi.fn()} />, {
      identity: { ...DANA, method: 'password', verified: false, name: null, email: null },
    })
    expect(screen.getByText(/unverified: shared demo password/)).toBeInTheDocument()
  })

  it('audit panel names actors by how they authenticated', () => {
    const records: AuditRecord[] = [
      {
        seq: 2,
        at: '2026-10-29T12:01:00Z',
        event: 'access_revoked',
        actor: { method: 'oidc', verified: true, email: 'dana@customer.example' },
        detail: { reason: 'Okta no longer vouches for this session' },
        sha256: 'c'.repeat(64),
      },
      {
        seq: 1,
        at: '2026-10-29T12:00:00Z',
        event: 'mapping_resolved',
        actor: { method: 'password', verified: false },
        detail: { kind: 'field', source: 'ZIP', status: 'edited' },
        sha256: 'd'.repeat(64),
      },
    ]
    render(<AuditPanel records={records} />)
    const rows = screen.getAllByRole('listitem').map((li) => li.textContent)
    expect(rows[0]).toContain('dana@customer.example access revoked by the IdP')
    expect(rows[1]).toContain('demo approver (unverified) resolved a mapping')
    expect(rows[1]).toContain('field ZIP → edited')
  })
})
