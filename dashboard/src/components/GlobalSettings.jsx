import React, { useEffect, useState } from 'react'
import { api } from '../api.js'
import { useLoad } from '../hooks/usePolling.js'
import { fmtDateTime } from '../util.js'
import { Chip, Empty, ErrorNote, Spinner, useAction } from './Common.jsx'

export function ValuesEditor() {
  const cfg = useLoad(api.getConfig, [])
  const [text, setText] = useState('')
  const [saved, setSaved] = useState(false)
  const { busy, error, run } = useAction()

  useEffect(() => { if (cfg.data) setText(cfg.data.values_principles || '') }, [cfg.data])

  const save = () => run(async () => {
    await api.putConfig({ ...cfg.data, values_principles: text })
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
      <h2>Previous swarms</h2>
      {list.loading ? <Spinner label="Loading" /> : list.error ? <ErrorNote error={list.error} /> :
        !swarms.length ? <Empty>No archived swarms yet.</Empty> : (
          <table className="table">
            <thead><tr><th>Name</th><th>State</th><th>Created</th><th /></tr></thead>
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
          </table>
        )}
      <ErrorNote error={error} />
    </div>
  )
}
