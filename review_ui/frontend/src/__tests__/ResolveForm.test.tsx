import { fireEvent, render, screen } from '@testing-library/react'
import ResolveForm from '../components/ResolveForm'
import { view } from './helpers'

describe('ResolveForm', () => {
  it('disables Save as edited until a target is entered, then submits it', () => {
    const onResolve = vi.fn().mockResolvedValue(undefined)
    render(<ResolveForm mapping={view({ transform: '' })} onResolve={onResolve} />)

    const save = screen.getByRole('button', { name: 'Save as edited' })
    expect(save).toBeDisabled()

    fireEvent.change(screen.getByLabelText('Target field'), {
      target: { value: 'attribs.zip' },
    })
    fireEvent.change(screen.getByLabelText('Transform / decision'), {
      target: { value: 'Left-pad to 5 digits.' },
    })
    fireEvent.click(save)
    expect(onResolve).toHaveBeenCalledWith({
      status: 'edited',
      target: 'attribs.zip',
      transform: 'Left-pad to 5 digits.',
    })
  })

  it('offers Accept as proposed only on proposed mappings and sends no target', () => {
    const onResolve = vi.fn().mockResolvedValue(undefined)
    render(
      <ResolveForm
        mapping={view({ status: 'proposed', openQuestion: null, target: 'attribs.state' })}
        onResolve={onResolve}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Accept as proposed' }))
    expect(onResolve).toHaveBeenCalledWith({ status: 'approved' })
  })

  it('does not offer Accept as proposed on a flagged mapping', () => {
    render(<ResolveForm mapping={view()} onResolve={vi.fn()} />)
    expect(screen.queryByRole('button', { name: 'Accept as proposed' })).not.toBeInTheDocument()
  })

  it('surfaces a rejection from the backend', async () => {
    const onResolve = vi.fn().mockRejectedValue(new Error('spec at x does not validate'))
    render(<ResolveForm mapping={view()} onResolve={onResolve} />)
    fireEvent.change(screen.getByLabelText('Target field'), { target: { value: 'attribs.zip' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save as edited' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('spec at x does not validate')
  })
})
