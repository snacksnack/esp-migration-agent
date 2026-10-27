import { useState, type FormEvent } from 'react'
import { ApiError, login } from '../api'
import './Login.css'

interface LoginProps {
  onSuccess: () => void
}

// The RC1-561 demo gate: one shared password (school-search pattern). The
// backend sets an HMAC session cookie; everything else about the review flow
// is identical once inside.
export default function Login({ onSuccess }: LoginProps) {
  const [password, setPassword] = useState('')
  const [failed, setFailed] = useState(false)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    try {
      await login(password)
      onSuccess()
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) setFailed(true)
      else throw err
    }
  }

  return (
    <div className="lg-wrap">
      <form className="lg-card" onSubmit={(e) => void submit(e)}>
        <h1>Review gate</h1>
        <p className="lg-blurb">
          A demo of the ESP Migration Agent's human review gate. Password, please.
        </p>
        <input
          type="password"
          autoFocus
          value={password}
          onChange={(e) => {
            setPassword(e.target.value)
            setFailed(false)
          }}
          placeholder="Password"
          aria-label="Password"
        />
        {failed && (
          <p className="lg-error" role="alert">
            That's not it — try again.
          </p>
        )}
        <button type="submit" disabled={!password}>
          Enter
        </button>
      </form>
    </div>
  )
}
