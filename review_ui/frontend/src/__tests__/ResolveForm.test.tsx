import { fireEvent, render, screen } from '@testing-library/react'
import { ApiError } from '../api'
import ResolveForm from '../components/ResolveForm'
import { view } from './helpers'

const DEMO_401 =
  'This is a live demo: anyone can review, but saving a change to the ' +
  'migration spec needs the approver sign-in.'

describe('ResolveForm', () => {
  it('disables Save as edited until a target is entered, then submits it', () => {
    const onResolve = vi.fn().mockResolvedValue(undefined)
    render(<ResolveForm mapping={view({ transform: '' })} onResolve={onResolve} onLogin={vi.fn()} />)

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
        onLogin={vi.fn()}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Accept as proposed' }))
    expect(onResolve).toHaveBeenCalledWith({ status: 'approved' })
  })

  it('does not offer Accept as proposed on a flagged mapping', () => {
    render(<ResolveForm mapping={view()} onResolve={vi.fn()} onLogin={vi.fn()} />)
    expect(screen.queryByRole('button', { name: 'Accept as proposed' })).not.toBeInTheDocument()
  })

  it('surfaces a non-auth rejection from the backend', async () => {
    const onResolve = vi.fn().mockRejectedValue(new ApiError(422, 'spec at x does not validate'))
    render(<ResolveForm mapping={view()} onResolve={onResolve} onLogin={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('Target field'), { target: { value: 'attribs.zip' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save as edited' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('spec at x does not validate')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('opens the demo sign-in dialog on a 401 and retries the save after login', async () => {
    const onResolve = vi
      .fn()
      .mockRejectedValueOnce(new ApiError(401, DEMO_401))
      .mockResolvedValueOnce(undefined)
    const onLogin = vi.fn().mockResolvedValue({ ok: true })
    render(<ResolveForm mapping={view()} onResolve={onResolve} onLogin={onLogin} />)
    fireEvent.change(screen.getByLabelText('Target field'), { target: { value: 'attribs.zip' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save as edited' }))

    const dialog = await screen.findByRole('dialog', { name: 'Approver sign-in' })
    expect(dialog).toHaveTextContent(DEMO_401)
    fireEvent.change(screen.getByLabelText('Approver password'), {
      target: { value: 'migrate-xyz' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Sign in and continue' }))

    await vi.waitFor(() => expect(onResolve).toHaveBeenCalledTimes(2))
    expect(onLogin).toHaveBeenCalledWith('migrate-xyz')
    expect(onResolve).toHaveBeenLastCalledWith(
      expect.objectContaining({ status: 'edited', target: 'attribs.zip' }),
    )
  })

  it('Keep browsing dismisses the dialog without retrying', async () => {
    const onResolve = vi.fn().mockRejectedValue(new ApiError(401, DEMO_401))
    render(<ResolveForm mapping={view()} onResolve={onResolve} onLogin={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('Target field'), { target: { value: 'attribs.zip' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save as edited' }))
    await screen.findByRole('dialog', { name: 'Approver sign-in' })
    fireEvent.click(screen.getByRole('button', { name: 'Keep browsing' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(onResolve).toHaveBeenCalledTimes(1)
  })
})
