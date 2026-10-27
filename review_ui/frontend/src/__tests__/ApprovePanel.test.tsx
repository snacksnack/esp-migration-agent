import { fireEvent, render, screen } from '@testing-library/react'
import { ApiError } from '../api'
import ApprovePanel from '../components/ApprovePanel'

const REFUSAL =
  "cannot approve: 2 unresolved mapping(s) (field 'ZIP', field 'LASTPURCH'). " +
  'Resolve each to approved/edited in the YAML, answering any open questions.'

const RECEIPT = {
  out: 'specs/approved/mailchimp_to_listmonk.yaml',
  approved_by: 'Reid Collins',
  approved_at: '2026-10-27T12:00:00+00:00',
  content_sha256: 'a'.repeat(64),
}

const typeOwner = (name: string) =>
  fireEvent.change(screen.getByLabelText('Approving owner'), { target: { value: name } })

describe('ApprovePanel', () => {
  it('disables approval without a named owner', () => {
    render(<ApprovePanel openQuestions={0} onApprove={vi.fn()} onLogin={vi.fn()} />)
    expect(screen.getByRole('button', { name: 'Approve spec' })).toBeDisabled()
  })

  it("renders the gate's refusal verbatim - no login involved", async () => {
    const onApprove = vi.fn().mockRejectedValue(new ApiError(409, REFUSAL))
    render(<ApprovePanel openQuestions={2} onApprove={onApprove} onLogin={vi.fn()} />)
    typeOwner('Visitor')
    fireEvent.click(screen.getByRole('button', { name: 'Approve spec' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(REFUSAL)
    expect(screen.queryByLabelText('Approver password')).not.toBeInTheDocument()
  })

  it('reveals the password step on a 401 and signs off after login', async () => {
    const onApprove = vi
      .fn()
      .mockRejectedValueOnce(
        new ApiError(401, 'every gate passes - but signing off needs the approver login'),
      )
      .mockResolvedValueOnce(RECEIPT)
    const onLogin = vi.fn().mockResolvedValue({ ok: true })
    render(<ApprovePanel openQuestions={0} onApprove={onApprove} onLogin={onLogin} />)
    typeOwner('Reid Collins')
    fireEvent.click(screen.getByRole('button', { name: 'Approve spec' }))

    expect(await screen.findByText(/signing off needs the approver login/)).toBeInTheDocument()
    const pw = screen.getByLabelText('Approver password')
    fireEvent.change(pw, { target: { value: 'migrate-xyz' } })
    fireEvent.click(screen.getByRole('button', { name: 'Sign in and approve' }))

    expect(await screen.findByText(RECEIPT.out)).toBeInTheDocument()
    expect(onLogin).toHaveBeenCalledWith('migrate-xyz')
    expect(onApprove).toHaveBeenLastCalledWith('Reid Collins')
  })

  it('surfaces a wrong password without losing the panel', async () => {
    const onApprove = vi.fn().mockRejectedValue(new ApiError(401, 'needs the approver login'))
    const onLogin = vi.fn().mockRejectedValue(new ApiError(401, 'wrong password'))
    render(<ApprovePanel openQuestions={0} onApprove={onApprove} onLogin={onLogin} />)
    typeOwner('Reid Collins')
    fireEvent.click(screen.getByRole('button', { name: 'Approve spec' }))
    fireEvent.change(await screen.findByLabelText('Approver password'), {
      target: { value: 'nope' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Sign in and approve' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('wrong password')
    expect(screen.getByLabelText('Approver password')).toBeInTheDocument()
  })
})
