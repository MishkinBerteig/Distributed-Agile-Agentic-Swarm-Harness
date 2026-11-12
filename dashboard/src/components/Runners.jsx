import React, { useState } from 'react'
import { api } from '../api.js'
import { usePolling } from '../hooks/usePolling.js'
import { ago } from '../util.js'
import { Empty, ErrorNote, Spinner, useAction } from './Common.jsx'

const origin = () => {
  const o = typeof window !== 'undefined' ? window.location.origin : ''
  // The dashboard dev server is not the coordinator; the coordinator listens on 8000 by default.
  return !o || /:(7173|5173)$/.test(o) ? 'http://localhost:8000' : o
}

function Harnesses({ list }) {
  if (!list || !list.length) return <span className="muted">none</span>
  return list.map((h) => (
    <span key={h.harness} className="chip" title={((h.models || []).length ? `models: ${h.models.join(', ')}` : 'any model')
      + ((h.capabilities || []).length ? `; capabilities: ${h.capabilities.map((c) => String(c).replace(/_/g, ' ')).join(', ')}` : '')}>
      {h.harness}{(h.models || []).length ? ` (${h.models.length})` : ''}
      {(h.capabilities || []).includes('run_code') && <span className="muted"> · run code</span>}
      {(h.capabilities || []).includes('web_research') && <span className="muted"> · web</span>}
    </span>
  ))
}

function NewToken({ created, onDismiss }) {
  const [copied, setCopied] = useState(false)
  const cmd = `python3 -m daash_runner --coordinator ${origin()} --token ${created.token} --config runner.json`
  const copy = async () => {
    try { await navigator.clipboard.writeText(created.token); setCopied(true) } catch { setCopied(false) }
  }
  return (
    <div className="warn-note token-box" role="status">
      <strong>Runner "{created.runner.name}" created.</strong> Copy the token now; it is shown only once.
      <div className="row">
        <code className="token grow">{created.token}</code>
        <button type="button" className="btn small" onClick={copy}>{copied ? 'Copied' : 'Copy token'}</button>
        <button type="button" className="btn small" onClick={onDismiss}>Dismiss</button>
      </div>
      <div>Run it on any device with Python 3.10+:</div>
      <pre className="cmd">{cmd}</pre>
      <div className="muted">Or use the Docker runner service (compose profile <code>runner</code>) with
        DAASH_RUNNER_BOOTSTRAP_TOKEN, so CLI agents run inside a container.</div>
    </div>
  )
}

export default function Runners() {
  const runners = usePolling(() => api.listRunners(), [], { interval: 5000 })
  const [name, setName] = useState('')
  const [adding, setAdding] = useState(false)
  const [created, setCreated] = useState(null)
  const { busy, error, run } = useAction()
  const list = (runners.data && runners.data.runners) || []

  const add = (e) => {
    e.preventDefault()
    run(async () => {
      setCreated(await api.createRunner(name.trim()))
      setName(''); setAdding(false); runners.refresh()
    })
  }
  const toggle = (r) => run(async () => { await api.patchRunner(r.id, { enabled: !r.enabled }); runners.refresh() })
  const remove = (r) => {
    if (!window.confirm(`Delete runner "${r.name}"? Its token stops working and its leased jobs are requeued.`)) return
    run(async () => { await api.deleteRunner(r.id); runners.refresh() })
  }

  return (
    <section className="card runners" aria-label="Runners">
      <div className="head-top">
        <h2>Runners</h2>
        {!adding && <button type="button" className="btn small" onClick={() => setAdding(true)}>Add runner</button>}
      </div>
      {adding && (
        <form className="row" onSubmit={add}>
          <label className="grow">Runner name
            <input required autoFocus value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. mac-mini" /></label>
          <button className="btn primary small" disabled={busy || !name.trim()}>Create</button>
          <button type="button" className="btn small" onClick={() => setAdding(false)}>Cancel</button>
        </form>
      )}
      {created && <NewToken created={created} onDismiss={() => setCreated(null)} />}
      <ErrorNote error={error} />
      {runners.error && runners.data === undefined ? <ErrorNote error={runners.error} /> : null}
      {runners.loading && runners.data === undefined ? <Spinner label="Loading runners" /> : !list.length ? (
        <Empty>No runners registered. Without one, remote harness work falls back to the built-in harness.</Empty>
      ) : (
        <div className="table-scroll">
          <table className="table compact">
            <thead><tr><th>Name</th><th>Host</th><th>Agents</th><th>Jobs</th><th>Last seen</th><th><span className="sr-only">Actions</span></th></tr></thead>
            <tbody>
              {list.map((r) => (
                <tr key={r.id} className={r.enabled ? '' : 'dim'}>
                  <td><span className={`dot ${r.online ? 'on' : 'off'}`} role="img"
                    aria-label={r.online ? 'online' : 'offline'} title={r.online ? 'online' : 'offline'} /> {r.name}
                    {!r.enabled && <span className="chip"> disabled</span>}</td>
                  <td>{r.host || '-'}</td>
                  <td><Harnesses list={r.harnesses} /></td>
                  <td>{r.active_jobs ?? 0}/{r.max_jobs}</td>
                  <td>{ago(r.last_seen_at)}</td>
                  <td className="nowrap">
                    <button type="button" className="btn small" disabled={busy} onClick={() => toggle(r)}>
                      {r.enabled ? 'Disable' : 'Enable'}</button>{' '}
                    <button type="button" className="btn small danger" disabled={busy} onClick={() => remove(r)}>Delete</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
