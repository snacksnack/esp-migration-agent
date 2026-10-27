import { render, screen } from '@testing-library/react'
import BarChart from '../components/BarChart'

describe('BarChart', () => {
  it('renders one labelled row per entry with formatted values', () => {
    render(
      <BarChart
        title="Consent mix"
        rows={[
          ['subscribed', 3756],
          ['unsubscribed', 411],
        ]}
      />,
    )
    expect(screen.getByRole('img', { name: 'Consent mix' })).toBeInTheDocument()
    expect(screen.getByText('subscribed')).toBeInTheDocument()
    expect(screen.getByText('3,756')).toBeInTheDocument()
    expect(screen.getByText('411')).toBeInTheDocument()
  })
})
