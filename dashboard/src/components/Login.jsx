import React, { useState } from 'react'
import { api, setToken } from '../api.js'
import { ErrorNote, Spinner, useAction } from './Common.jsx'

export default function Login({ expired, onSignedIn }) {
  const [token, setTok] = useState('')
  const { busy, error, run } = useAction()
  const submit = (e) => {
    e.preventDefault()
    run(async () => {
      setToken(token.trim())
      try { await api.getConfig() } catch (err) {
        if (err.status === 401) { setToken(''); throw new Error('That token was not accepted.') }
        throw err
      }
      onSignedIn()
    })
  }
  return (
    <main className="container">
      <form className="card form login" onSubmit={submit}>
        <h2>Sign in</h2>
        <p className="muted">
          {expired ? 'The coordinator rejected the saved token. ' : ''}
          This coordinator requires an API token (DAASH_API_TOKEN).
        </p>
        <label>API token
          <input type="password" autoComplete="current-password" autoFocus required value={token}
            onChange={(e) => setTok(e.target.value)} /></label>
        <ErrorNote error={error} />
        <div><button className="btn primary" disabled={busy || !token.trim()}>
          {busy ? <Spinner label="Checking" /> : 'Sign in'}</button></div>
      </form>
    </main>
  )
}
