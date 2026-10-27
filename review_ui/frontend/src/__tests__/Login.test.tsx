import { fireEvent, render, screen } from '@testing-library/react'
import Login from '../components/Login'

const stubLogin = (status: number, body: unknown) =>
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue(new Response(JSON.stringify(body), { status })),
  )

afterEach(() => vi.unstubAllGlobals())

describe('Login', () => {
  it('disables Enter until a password is typed', () => {
    render(<Login onSuccess={vi.fn()} />)
    expect(screen.getByRole('button', { name: 'Enter' })).toBeDisabled()
  })

  it('calls onSuccess when the password is accepted', async () => {
    stubLogin(200, { ok: true })
    const onSuccess = vi.fn()
    render(<Login onSuccess={onSuccess} />)
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'let-me-in' } })
    fireEvent.click(screen.getByRole('button', { name: 'Enter' }))
    await vi.waitFor(() => expect(onSuccess).toHaveBeenCalled())
  })

  it('shows the retry message on a wrong password and clears it on edit', async () => {
    stubLogin(401, { detail: 'wrong password' })
    render(<Login onSuccess={vi.fn()} />)
    const input = screen.getByLabelText('Password')
    fireEvent.change(input, { target: { value: 'nope' } })
    fireEvent.click(screen.getByRole('button', { name: 'Enter' }))
    expect(await screen.findByRole('alert')).toHaveTextContent("That's not it")
    fireEvent.change(input, { target: { value: 'nope2' } })
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
})
