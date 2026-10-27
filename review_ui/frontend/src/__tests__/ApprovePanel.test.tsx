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

  it("renders the gate's refusal as a list - no sign-in involved", async () => {
    const onApprove = vi.fn().mockRejectedValue(new ApiError(409, REFUSAL))
    render(<ApprovePanel openQuestions={2} onApprove={onApprove} onLogin={vi.fn()} />)
    typeOwner('Visitor')
    fireEvent.click(screen.getByRole('button', { name: 'Approve spec' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'cannot approve: 2 unresolved mappings',
    )
    expect(screen.getAllByRole('listitem').map((li) => li.textContent)).toEqual([
      "field 'ZIP'",
      "field 'LASTPURCH'",
    ])
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('opens the sign-in dialog on a 401 and signs off after login', async () => {
    const onApprove = vi
      .fn()
      .mockRejectedValueOnce(
        new ApiError(401, 'every gate passes - but signing off needs the approver sign-in'),
      )
      .mockResolvedValueOnce(RECEIPT)
    const onLogin = vi.fn().mockResolvedValue({ ok: true })
    render(<ApprovePanel openQuestions={0} onApprove={onApprove} onLogin={onLogin} />)
    typeOwner('Reid Collins')
    fireEvent.click(screen.getByRole('button', { name: 'Approve spec' }))

    const dialog = await screen.findByRole('dialog', { name: 'Approver sign-in' })
    expect(dialog).toHaveTextContent(/signing off needs the approver sign-in/)
    fireEvent.change(screen.getByLabelText('Approver password'), {
      target: { value: 'migrate-xyz' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Sign in and continue' }))

    expect(await screen.findByText(RECEIPT.out)).toBeInTheDocument()
    expect(onLogin).toHaveBeenCalledWith('migrate-xyz')
    expect(onApprove).toHaveBeenLastCalledWith('Reid Collins')
  })

  it('a wrong password shows inside the dialog without losing it', async () => {
    const onApprove = vi.fn().mockRejectedValue(new ApiError(401, 'needs the approver sign-in'))
    const onLogin = vi.fn().mockRejectedValue(new ApiError(401, 'wrong password'))
    render(<ApprovePanel openQuestions={0} onApprove={onApprove} onLogin={onLogin} />)
    typeOwner('Reid Collins')
    fireEvent.click(screen.getByRole('button', { name: 'Approve spec' }))
    fireEvent.change(await screen.findByLabelText('Approver password'), {
      target: { value: 'nope' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Sign in and continue' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('wrong password')
    expect(screen.getByRole('dialog', { name: 'Approver sign-in' })).toBeInTheDocument()
  })
})
