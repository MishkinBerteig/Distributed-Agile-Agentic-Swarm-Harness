import React, { useCallback, useEffect, useState } from 'react'
import { api, getToken, onAuthRequired, setToken } from './api.js'
import { usePolling } from './hooks/usePolling.js'
import { ErrorNote, Spinner } from './components/Common.jsx'
import CreateSwarm from './components/CreateSwarm.jsx'
import { PreviousSwarms, ValuesEditor } from './components/GlobalSettings.jsx'
import LiveSwarm from './components/LiveSwarm.jsx'
import Login from './components/Login.jsx'
import Runners from './components/Runners.jsx'

function Main() {
  const active = usePolling(() => api.getActiveSwarm(), [])
  const swarm = active.data
  return (
    <>
      <main className="container">
        {active.loading && swarm === undefined ? <Spinner label="Connecting to coordinator" /> : null}
        {active.error && swarm === undefined && !active.loading && (
          <ErrorNote error={new Error(`Cannot load the active swarm: ${active.error.message}. Retrying every 3 seconds.`)} />
        )}
        {swarm ? (
          <>
            <LiveSwarm key={swarm.id} swarm={swarm} onSwarmChanged={active.refresh} />
            <Runners />
          </>
        ) : swarm === null ? (
          <div className="idle">
            <div className="idle-main"><CreateSwarm onCreated={active.refresh} /></div>
            <div className="idle-side">
              <Runners />
              <ValuesEditor />
              <PreviousSwarms activeId={null} />
            </div>
          </div>
        ) : null}
      </main>
    </>
  )
}

export default function App() {
  // 'checking' | 'login' | 'expired' | 'ok'
  const [auth, setAuth] = useState('checking')
  const [authRequired, setAuthRequired] = useState(false)

  const check = useCallback(async () => {
    try {
      const h = await api.health()
      setAuthRequired(Boolean(h && h.auth_required))
      setAuth(h && h.auth_required && !getToken() ? 'login' : 'ok')
    } catch {
      // Coordinator down: show the app (it retries); a 401 later flips to the login screen.
      setAuth('ok')
    }
  }, [])

  useEffect(() => {
    onAuthRequired(() => { setAuthRequired(true); setAuth((a) => (a === 'login' ? a : 'expired')) })
    check()
    return () => onAuthRequired(null)
  }, [check])

  const signOut = () => { setToken(''); setAuth('login') }
  const showSignOut = (authRequired || Boolean(getToken())) && auth === 'ok'

  return (
    <div className="app">
      <nav className="topbar">
        <span className="brand">DAASHboard</span>
        <span className="muted">Distributed Agile Agentic Swarm Harness</span>
        {showSignOut && <button type="button" className="btn small signout" onClick={signOut}>Sign out</button>}
      </nav>
      {auth === 'checking' ? (
        <main className="container"><Spinner label="Connecting to coordinator" /></main>
      ) : auth === 'ok' ? (
        <Main />
      ) : (
        <Login expired={auth === 'expired'} onSignedIn={() => { setAuth('ok'); check() }} />
      )}
    </div>
  )
}
