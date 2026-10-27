import { render, screen } from '@testing-library/react'
import MappingCard from '../components/MappingCard'
import { view } from './helpers'

describe('MappingCard', () => {
  it('shows the open question, confidence and status for a flagged mapping', () => {
    render(<MappingCard mapping={view()} onResolve={vi.fn()} onLogin={vi.fn()} />)
    expect(screen.getByText('How should the truncated ZIPs be handled?')).toBeInTheDocument()
    expect(screen.getByText('30%')).toBeInTheDocument()
    expect(screen.getByText('flagged')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Review' })).toBeInTheDocument()
  })

  it('shows the target and no review button once resolved', () => {
    render(
      <MappingCard
        mapping={view({ status: 'edited', openQuestion: null, target: 'attribs.zip' })}
        onResolve={vi.fn()}
        onLogin={vi.fn()}
      />,
    )
    expect(screen.getByText('attribs.zip')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Review' })).not.toBeInTheDocument()
  })
})
