import { fireEvent, render, screen } from '@testing-library/react'
import ApprovePanel from '../components/ApprovePanel'

const REFUSAL =
  "cannot approve: 2 unresolved mapping(s) (field 'ZIP', field 'LASTPURCH'). " +
  'Resolve each to approved/edited in the YAML, answering any open questions.'

describe('ApprovePanel', () => {
  it('disables approval without a named owner', () => {
    render(<ApprovePanel openQuestions={0} onApprove={vi.fn()} />)
    expect(screen.getByRole('button', { name: 'Approve spec' })).toBeDisabled()
  })

  it("renders the gate's refusal verbatim", async () => {
    const onApprove = vi.fn().mockRejectedValue(new Error(REFUSAL))
    render(<ApprovePanel openQuestions={2} onApprove={onApprove} />)
    fireEvent.change(screen.getByLabelText('Approving owner'), {
      target: { value: 'Reid Collins' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Approve spec' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(REFUSAL)
    expect(onApprove).toHaveBeenCalledWith('Reid Collins')
  })

  it('shows the approval receipt on success', async () => {
    const onApprove = vi.fn().mockResolvedValue({
      out: 'specs/approved/mailchimp_to_listmonk.yaml',
      approved_by: 'Reid Collins',
      approved_at: '2026-10-27T12:00:00+00:00',
      content_sha256: 'a'.repeat(64),
    })
    render(<ApprovePanel openQuestions={0} onApprove={onApprove} />)
    fireEvent.change(screen.getByLabelText('Approving owner'), {
      target: { value: 'Reid Collins' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Approve spec' }))
    expect(await screen.findByText('specs/approved/mailchimp_to_listmonk.yaml')).toBeInTheDocument()
    expect(screen.getByText('a'.repeat(64))).toBeInTheDocument()
  })
})
