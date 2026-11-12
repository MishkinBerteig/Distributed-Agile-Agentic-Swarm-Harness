import React, { useEffect, useState } from 'react'
import { api } from '../api.js'
import { useLoad } from '../hooks/usePolling.js'
import { fmtDateTime } from '../util.js'
import { Chip, Empty, ErrorNote, Spinner, useAction } from './Common.jsx'
import SwarmConfigFields from './SwarmConfigFields.jsx'

export function ValuesEditor() {
  const cfg = useLoad(api.getConfig, [])
  const [text, setText] = useState('')
  const [saved, setSaved] = useState(false)
  const { busy, error, run } = useAction()

  useEffect(() => { if (cfg.data) setText(cfg.data.values_principles || '') }, [cfg.data])

  // Re-read before saving so a change made elsewhere (Defaults for new swarms) is not undone.
  const save = () => run(async () => {
    await api.putConfig({ ...(await api.getConfig()), values_principles: text })
    setSaved(true); setTimeout(() => setSaved(false), 2000)
    cfg.refresh()
  })

  return (
    <div className="card form">
      <h2>Values &amp; Principles</h2>
      <p className="muted">Global guidance given to every agent in every swarm.</p>
      {cfg.loading ? <Spinner label="Loading" /> : cfg.error && !cfg.data ? <ErrorNote error={cfg.error} /> : (
        <>
          <textarea rows={6} value={text} onChange={(e) => setText(e.target.value)} />
          <ErrorNote error={error} />
          <div className="row">
            <button className="btn primary" onClick={save} disabled={busy}>{busy ? <Spinner label="Saving" /> : 'Save'}</button>
            {saved && <span className="ok-text">Saved</span>}
          </div>
        </>
      )}
    </div>
  )
}

// Settings: defaults every new swarm starts from (Create a swarm can still change them).
export function DefaultsEditor() {
  const cfg = useLoad(api.getConfig, [])
  const models = useLoad(api.getModels, [])
  const avail = useLoad(api.getAvailability, [])
  const [draft, setDraft] = useState(null)
  const [saved, setSaved] = useState(false)
  const { busy, error, run } = useAction()

  useEffect(() => { if (cfg.data) setDraft(structuredClone(cfg.data.default_swarm_config)) }, [cfg.data])
  const changed = draft && cfg.data && JSON.stringify(draft) !== JSON.stringify(cfg.data.default_swarm_config)

  const save = () => run(async () => {
    await api.putConfig({ ...(await api.getConfig()), default_swarm_config: draft })
    setSaved(true); setTimeout(() => setSaved(false), 2000)
    cfg.refresh()
  })

  return (
    <div className="card form">
      <h2>Defaults for new swarms</h2>
      <p className="muted">Models, agents and limits every new swarm starts with. Create a swarm can change them for one
        swarm. A running swarm keeps the settings it was created with.</p>
      {!draft ? (cfg.error ? <ErrorNote error={cfg.error} /> : <Spinner label="Loading" />) : (
        <>
          <SwarmConfigFields cfg={draft} setCfg={setDraft} gatewayModels={(models.data && models.data.models) || []}
            harnesses={avail.data && avail.data.harnesses} />
          <ErrorNote error={error} />
          <div className="row">
            <button className="btn primary" onClick={save} disabled={busy || !changed}>{busy ? <Spinner label="Saving" /> : 'Save defaults'}</button>
            <button className="btn" onClick={() => setDraft(structuredClone(cfg.data.default_swarm_config))} disabled={busy || !changed}>Undo changes</button>
            {saved && <span className="ok-text">Saved</span>}
          </div>
        </>
      )}
    </div>
  )
}

export function SettingsPage() {
  return (
    <main className="container page">
      <h1 className="page-title">Settings</h1>
      <p className="page-lede muted">Global settings for every swarm. Runners are under <a href="#/agents">Agents &amp; models</a>.</p>
      <div className="settings">
        <ValuesEditor />
        <DefaultsEditor />
      </div>
    </main>
  )
}

export function PreviousSwarms({ activeId }) {
  const list = useLoad(api.listSwarms, [activeId])
  const { busy, error, run } = useAction()
  const swarms = ((list.data && list.data.swarms) || []).filter((s) => s.id !== activeId)

  const del = (s) => {
    if (!window.confirm(`Delete swarm "${s.name}"? This removes its data and workspace.`)) return
    run(async () => { await api.deleteSwarm(s.id); list.refresh() })
  }

  return (
    <div className="card">
      {list.loading ? <Spinner label="Loading" /> : list.error ? <ErrorNote error={list.error} /> :
        !swarms.length ? <Empty>No archived swarms yet.</Empty> : (
          <div className="table-scroll"><table className="table">
            <thead><tr><th>Name</th><th>State</th><th>Created</th><th><span className="sr-only">Actions</span></th></tr></thead>
            <tbody>
              {swarms.map((s) => (
                <tr key={s.id}>
                  <td><strong>{s.name}</strong><div className="muted clip">{s.vision_statement}</div></td>
                  <td><Chip status={s.lifecycle_state} /></td>
                  <td className="muted">{fmtDateTime(s.created_at)}</td>
                  <td>{['CREATED', 'VERIFICATION', 'ARCHIVED'].includes(s.lifecycle_state) &&
                    <button className="btn small danger" disabled={busy} onClick={() => del(s)}>Delete</button>}</td>
                </tr>
              ))}
            </tbody>
          </table></div>
        )}
      <ErrorNote error={error} />
    </div>
  )
}
