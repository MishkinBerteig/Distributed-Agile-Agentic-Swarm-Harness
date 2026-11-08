import React from 'react'
import { api } from './api.js'
import { usePolling } from './hooks/usePolling.js'
import { ErrorNote, Spinner } from './components/Common.jsx'
import CreateSwarm from './components/CreateSwarm.jsx'
import { PreviousSwarms, ValuesEditor } from './components/GlobalSettings.jsx'
import LiveSwarm from './components/LiveSwarm.jsx'

export default function App() {
  const active = usePolling(() => api.getActiveSwarm(), [])
  const swarm = active.data

  return (
    <div className="app">
      <nav className="topbar">
        <span className="brand">DAASHboard</span>
        <span className="muted">Distributed Agile Agentic Swarm Harness</span>
        {active.error && <span className="conn-bad">coordinator unreachable</span>}
      </nav>
      <main className="container">
        {active.loading && swarm === undefined ? <Spinner label="Connecting to coordinator" /> : null}
        {active.error && swarm === undefined && !active.loading && (
          <ErrorNote error={new Error(`Cannot load the active swarm: ${active.error.message}. Retrying every 3 seconds.`)} />
        )}
        {swarm ? (
          <>
            <LiveSwarm key={swarm.id} swarm={swarm} onSwarmChanged={active.refresh} />
          </>
        ) : swarm === null ? (
          <div className="idle">
            <div className="idle-main"><CreateSwarm onCreated={active.refresh} /></div>
            <div className="idle-side">
              <ValuesEditor />
              <PreviousSwarms activeId={null} />
            </div>
          </div>
        ) : null}
      </main>
    </div>
  )
}
