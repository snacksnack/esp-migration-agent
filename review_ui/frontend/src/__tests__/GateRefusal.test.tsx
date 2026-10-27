import { render, screen } from '@testing-library/react'
import GateRefusal from '../components/GateRefusal'

const REFUSAL =
  "cannot approve: 16 unresolved mapping(s) (field 'EMAIL', field 'FNAME', " +
  "field 'LNAME', field 'ZIP', field 'STATE', field 'LASTPURCH', " +
  "field 'MEMBER_RATING', field 'OPTIN_TIME', field 'CONFIRM_TIME', " +
  "segment 'Brooklyn subscribers', segment 'Highly engaged (4-5 stars)', " +
  "segment 'VIP customers', segment 'VIP (legacy)', status 'subscribed', " +
  "status 'unsubscribed', status 'nonsubscribed'). " +
  'Resolve each to approved/edited in the YAML, answering any open questions.'

describe('GateRefusal', () => {
  it('formats the known refusal shape as headline, bullets, instruction', () => {
    render(<GateRefusal detail={REFUSAL} />)
    expect(screen.getByText('cannot approve: 16 unresolved mappings')).toBeInTheDocument()
    expect(screen.getAllByRole('listitem')).toHaveLength(16)
    expect(
      screen.getByText('Resolve each to approved/edited in the YAML, answering any open questions.'),
    ).toBeInTheDocument()
  })

  it('keeps commas and parens inside quoted names in one bullet', () => {
    render(<GateRefusal detail={REFUSAL} />)
    expect(screen.getByText("segment 'Highly engaged (4-5 stars)'")).toBeInTheDocument()
    expect(screen.getByText("segment 'VIP (legacy)'")).toBeInTheDocument()
  })

  it('renders anything else as plain text', () => {
    render(<GateRefusal detail="spec at specs/x.yaml does not validate: boom" />)
    expect(screen.getByRole('alert')).toHaveTextContent(
      'spec at specs/x.yaml does not validate: boom',
    )
    expect(screen.queryByRole('listitem')).not.toBeInTheDocument()
  })

  it('uses the singular for one unresolved mapping', () => {
    render(
      <GateRefusal
        detail={
          "cannot approve: 1 unresolved mapping(s) (field 'ZIP'). " +
          'Resolve each to approved/edited in the YAML, answering any open questions.'
        }
      />,
    )
    expect(screen.getByText('cannot approve: 1 unresolved mapping')).toBeInTheDocument()
    expect(screen.getAllByRole('listitem')).toHaveLength(1)
  })
})
