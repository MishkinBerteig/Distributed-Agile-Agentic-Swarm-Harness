import React from 'react'
import { api } from '../api.js'
import { usePolling } from '../hooks/usePolling.js'
import { HARNESS_STATUS, capLabel, statusText, unavailableAllowed } from '../util.js'
import { ErrorNote, Spinner } from './Common.jsx'
import Runners from './Runners.jsx'

// Agents & models: what a swarm can use right now (GET /availability), and the runners that
// provide the external agents.
export const Status = ({ status }) => {
  const s = HARNESS_STATUS[status] || { text: status, on: false }
  return <span className="nowrap"><span className={`dot ${s.on ? 'on' : 'off'}`} aria-hidden="true" />{s.text}</span>
}


function Use({ h, deliveryRows }) {
  if (!h.allowed) return <span className="muted">Not allowed</span>
  if (h.builtin) {
    return <>Swarm Coordinator and judges{deliveryRows ? '' : '; every delivery agent'}
      {h.delivery_rows ? `; ${h.delivery_rows} delivery row${h.delivery_rows > 1 ? 's' : ''}` : ''}</>
  }
  return h.delivery_rows
    ? <>{h.delivery_rows} delivery row{h.delivery_rows > 1 ? 's' : ''}</>
    : <span className="muted">Allowed, but no delivery row uses it</span>
}

function AgentsTable({ data }) {
  const rows = data.harnesses
  const deliveryRows = rows.reduce((n, h) => n + h.delivery_rows, 0)
  const missing = unavailableAllowed(rows)
  return (
    <section className="card" aria-labelledby="agents-h">
      <h2 id="agents-h">Agents</h2>
      <p className="muted">The Swarm Coordinator and both judges always use DAASH&rsquo;s built-in agent. Delivery
        agents use the agent types in the swarm&rsquo;s delivery rows; external ones run on a runner.</p>
      {missing.map((h) => (
        <div key={h.harness} className="warn-note">
          <strong>{h.label}</strong> is allowed, but {statusText(h.status).toLowerCase()}.
          {h.builtin ? ' Nothing can run until the gateway is back.'
            : ' Its work waits for a runner, then falls back to the built-in agent.'}
        </div>
      ))}
      <div className="table-scroll">
        <table className="table">
          <thead><tr><th>Agent</th><th>Status</th><th>Runs on</th><th>Can</th><th>Models</th>
            <th>{data.scope ? 'In this swarm' : 'In new swarms'}</th></tr></thead>
          <tbody>
            {rows.map((h) => (
              <tr key={h.harness}>
                <td><strong>{h.label}</strong><div className="muted"><code>{h.harness}</code></div></td>
                <td><Status status={h.status} /></td>
                <td>{h.builtin ? 'Inside DAASH' : h.runners.length ? h.runners.map((r) => (
                  <div key={r.id} className="nowrap"><span className={`dot ${r.online ? 'on' : 'off'}`} aria-hidden="true" />
                    {r.name}<span className="muted">{r.enabled ? (r.online ? '' : ' (offline)') : ' (disabled)'}</span></div>
                )) : <span className="muted">none</span>}</td>
                <td><div className="caps">{h.capabilities.map((c) => <span key={c} className="chip cap-chip">{capLabel(c)}</span>)}</div></td>
                <td>{h.models === null ? <span className="muted">any model</span>
                  : h.models.length ? h.models.join(', ') : <span className="muted">none</span>}</td>
                <td><Use h={h} deliveryRows={deliveryRows} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}

function ModelsTable({ data }) {
  const g = data.gateway
  return (
    <section className="card" aria-labelledby="models-h">
      <h2 id="models-h">Models</h2>
      <p>
        <span className={`dot ${g.reachable ? 'on' : 'off'}`} aria-hidden="true" />
        LLM gateway <code>{g.endpoint}</code> is {g.reachable ? 'reachable' : <strong className="bad">not reachable</strong>}.
      </p>
      <p className="muted">Listed in escalation order, cheapest first. Delivery work starts at its role&rsquo;s model and
        moves on to the next one for complex tasks, when past results show its model failing, and after each failed
        attempt. The names are the ones the gateway reports; it does not say which LLM is behind each name.</p>
      <div className="table-scroll">
        <table className="table">
          <thead><tr><th>Step</th><th>Model</th><th>Starts here</th><th>Gateway</th></tr></thead>
          <tbody>
            {data.models.map((m) => (
              <tr key={m.name} className={m.allowed ? '' : 'dim'}>
                <td>{m.step ?? (m.allowed ? 'not on the ladder' : 'not allowed')}</td>
                <td><code>{m.name}</code></td>
                <td>{m.roles.length ? m.roles.join(', ') : <span className="muted">-</span>}</td>
                <td>{m.offered === null ? <span className="muted">unknown</span>
                  : m.offered ? 'offered' : <span className="bad">not offered</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="muted">Change the order and the models allowed in <a href="#/settings">Settings</a> (for new swarms) or
        in Advanced configuration when you create a swarm.</p>
    </section>
  )
}

export default function AgentsPage() {
  const av = usePolling(() => api.getAvailability(), [], { interval: 10000 })
  const d = av.data
  return (
    <main className="container page">
      <h1 className="page-title">Agents &amp; models</h1>
      <p className="page-lede muted">
        {d ? (d.scope
          ? <>What the running swarm <strong>{d.scope.swarm_name}</strong> can use right now.</>
          : <>What a new swarm can use right now, with the defaults from <a href="#/settings">Settings</a>.</>)
          : 'What a swarm can use right now.'}
      </p>
      {av.error && !d && <ErrorNote error={av.error} />}
      {av.loading && !d ? <Spinner label="Loading" /> : d && (
        <>
          <AgentsTable data={d} />
          <ModelsTable data={d} />
        </>
      )}
      <Runners />
    </main>
  )
}
