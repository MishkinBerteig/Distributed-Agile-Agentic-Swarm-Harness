import React, { useCallback, useEffect, useState } from 'react'
import { api, getToken, onAuthRequired, setToken } from './api.js'
import { useLoad, usePolling } from './hooks/usePolling.js'
import { useRoute } from './router.js'
import { ErrorNote, Spinner } from './components/Common.jsx'
import AgentsPage, { Status } from './components/AgentsPage.jsx'
import { unavailableAllowed } from './util.js'
import CreateSwarm from './components/CreateSwarm.jsx'
import { PreviousSwarms, SettingsPage } from './components/GlobalSettings.jsx'
import Help from './components/Help.jsx'
import LiveSwarm from './components/LiveSwarm.jsx'
import Login from './components/Login.jsx'
import TopBar from './components/TopBar.jsx'

// Beside Create a swarm: what a new swarm could use right now, linking to the details.
function AvailableNow() {
  const av = useLoad(api.getAvailability, [])
  const d = av.data
  const missing = d ? unavailableAllowed(d.harnesses) : []
  return (
    <section className="card" aria-labelledby="avail-h">
      <h2 id="avail-h">Available now</h2>
      {av.error && !d ? <ErrorNote error={av.error} /> : !d ? <Spinner label="Loading" /> : (
        <>
          <ul className="avail-list">
            {d.harnesses.map((h) => (
              <li key={h.harness} className={h.allowed ? '' : 'dim'}>
                <span>{h.label}</span><Status status={h.status} />
              </li>
            ))}
          </ul>
          <p className="muted">Models: {d.models.filter((m) => m.allowed).map((m) => m.name).join(', ') || 'none'}
            {!d.gateway.reachable && <strong className="bad"> (LLM gateway not reachable)</strong>}</p>
          {missing.length > 0 && (
            <div className="warn-note">{missing.map((h) => h.label).join(', ')}: allowed by default, but not available now.</div>
          )}
          <a href="#/agents">Agents &amp; models: details and runners</a>
        </>
      )}
    </section>
  )
}

function SwarmPage({ active }) {
  const swarm = active.data
  return (
    <main className="container">
      {active.loading && swarm === undefined ? <Spinner label="Connecting to coordinator" /> : null}
      {active.error && swarm === undefined && !active.loading && (
        <ErrorNote error={new Error(`Cannot load the active swarm: ${active.error.message}. Retrying every 3 seconds.`)} />
      )}
      {swarm ? (
        <LiveSwarm key={swarm.id} swarm={swarm} onSwarmChanged={active.refresh} />
      ) : swarm === null ? (
        <div className="idle">
          <div className="idle-main"><CreateSwarm onCreated={active.refresh} /></div>
          <div className="idle-side"><AvailableNow /></div>
        </div>
      ) : null}
    </main>
  )
}

function SignedIn({ route, showSignOut, onSignOut }) {
  const active = usePolling(() => api.getActiveSwarm(), [])
  const swarm = active.data
  useEffect(() => { if (route.page !== 'help') window.scrollTo(0, 0) }, [route.page])
  return (
    <>
      <TopBar route={route} swarm={swarm} showSignOut={showSignOut} onSignOut={onSignOut} />
      {/* The swarm page stays mounted on other pages so a half-typed form or open panel survives. */}
      <div hidden={route.page !== 'swarm'}><SwarmPage active={active} /></div>
      {route.page === 'swarms' && (
        <main className="container page">
          <h1 className="page-title">Past swarms</h1>
          <PreviousSwarms activeId={swarm ? swarm.id : null} />
        </main>
      )}
      {route.page === 'agents' && <AgentsPage />}
      {route.page === 'settings' && <SettingsPage />}
      {route.page === 'help' && <Help section={route.sub} />}
    </>
  )
}

export default function App() {
  const route = useRoute()
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
      {auth === 'ok' ? (
        <SignedIn route={route} showSignOut={showSignOut} onSignOut={signOut} />
      ) : (
        <>
          <TopBar route={route} nav={false} />
          {auth === 'checking' ? (
            <main className="container"><Spinner label="Connecting to coordinator" /></main>
          ) : (
            <Login expired={auth === 'expired'} onSignedIn={() => { setAuth('ok'); check() }} />
          )}
        </>
      )}
    </div>
  )
}
