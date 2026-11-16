import React, { useEffect, useState } from 'react'
import { api } from '../api.js'
import { usePolling } from '../hooks/usePolling.js'
import { ago, fmtNum } from '../util.js'
import { Bar, ErrorNote, Spinner, useAction } from './Common.jsx'

// Settings > Search embeddings (docs/DESIGN-0.4.1.md section 6): which model turns texts into
// vectors for search, what the embedder service is doing, and a way to choose another model.

const OTHER = '__other__'
const TABLE_NAMES = { tasks: 'tasks', learnings: 'learnings', memories: 'memories' }

const sizeText = (m) => (m.size_mb ? (m.size_mb >= 1000 ? `${(m.size_mb / 1000).toFixed(1)} GB` : `${m.size_mb} MB`) : 'no download')
const modelLabel = (catalog, name) => {
  const m = (catalog || []).find((c) => c.name === name)
  return m ? `${m.label} (${m.name})` : name
}

function EmbedderStatus({ data }) {
  const { status = {}, service = {}, catalog, counts = {}, indexes = {} } = data
  const active = status.active_model
  const label = active ? modelLabel(catalog, active) : null
  const total = Object.values(counts).reduce((n, c) => n + c.total, 0)
  const indexed = Object.values(counts).reduce((n, c) => n + c.indexed, 0)
  const perTable = Object.entries(counts).map(([t, c]) => `${TABLE_NAMES[t] || t} ${fmtNum(c.indexed)}/${fmtNum(c.total)}`).join(' · ')
  const hnsw = Object.values(indexes).some((names) => names.length)

  if (!service.configured) {
    return (
      <div className="warn-note" role="status">No embedder service is set up (<code>DAASH_EMBEDDER_URL</code> is empty), so each
        DAASH process matches texts by word hashes: shared words, not meaning. <code>docker compose up -d</code> starts the
        embedder.</div>
    )
  }
  return (
    <div role="status">
      {!service.reachable && (
        <div className="warn-note">The embedder service is not answering. Until it is back, new items are stored without a
          vector and searches go by recency. {status.updated_at && <span className="muted">Last heard from {ago(status.updated_at)}.</span>}</div>
      )}
      {status.error && (
        <div className="warn-note"><strong>{status.error}</strong>{active && <> DAASH still uses {label}.</>}</div>
      )}
      {status.state === 'loading' ? (
        <div className="wait-banner"><Spinner /> <strong>Loading {modelLabel(catalog, status.chosen_model)}&hellip;</strong>
          <p>A model that is not built in is downloaded first, which can take a few minutes.
            {active ? ` Searches use ${label} until it is ready.` : ''}</p></div>
      ) : status.state === 'indexing' ? (
        <div className="wait-banner"><Spinner /> <strong>Embedding with {label}: {fmtNum(indexed)} of {fmtNum(total)} items</strong>
          <Bar label="" value={indexed} max={total || 1} text={perTable} />
          <p>Items not embedded yet are found by recency until they are.</p></div>
      ) : status.state === 'ready' && active ? (
        <div className="ok-banner"><strong>Ready &#10003;</strong> {label}, {status.dim} dimensions.
          <p>{fmtNum(indexed)} of {fmtNum(total)} items embedded ({perTable}).{' '}
            {hnsw ? 'PostgreSQL indexes them for nearest-neighbour search (pgvector HNSW).'
              : status.dim > 2000 ? 'Over 2000 dimensions: PostgreSQL searches them exactly, without an index.' : ''}</p></div>
      ) : status.state === 'error' ? (
        <div className="error-note">No embedding model could be loaded, so searches go by recency.</div>
      ) : (
        <div className="wait-banner"><Spinner /> <strong>The embedder is starting&hellip;</strong></div>
      )}
    </div>
  )
}

export default function EmbeddingSettings() {
  const emb = usePolling(api.getEmbeddings, [], { interval: 3000 })
  const [choice, setChoice] = useState(null)
  const [otherName, setOtherName] = useState('')
  const [trust, setTrust] = useState(false)
  const [saved, setSaved] = useState(false)
  const { busy, error, run } = useAction()
  const data = emb.data
  const catalog = (data && data.catalog) || []
  const setting = data && data.setting

  // Start from the saved choice (and again after saving).
  const settingKey = setting ? `${setting.model}|${setting.trust_remote_code}` : ''
  useEffect(() => {
    if (!setting) return
    const known = catalog.some((m) => m.name === setting.model)
    setChoice(known ? setting.model : OTHER)
    setOtherName(known ? '' : setting.model)
    setTrust(!known && !!setting.trust_remote_code)
  }, [settingKey]) // eslint-disable-line react-hooks/exhaustive-deps

  if (!data) {
    return (
      <div className="card form">
        <h2>Search embeddings</h2>
        {emb.error ? <ErrorNote error={emb.error} /> : <Spinner label="Loading" />}
      </div>
    )
  }

  const entry = catalog.find((m) => m.name === choice)
  const wanted = choice === OTHER ? otherName.trim() : choice
  const wantedTrust = choice === OTHER ? trust : false
  const changed = wanted && (wanted !== setting.model || (choice === OTHER && wantedTrust !== !!setting.trust_remote_code))
  const total = Object.values(data.counts || {}).reduce((n, c) => n + c.total, 0)

  const save = () => {
    if (!window.confirm(`Use ${modelLabel(catalog, wanted)} for search? DAASH loads it (downloading it if it is not built in) `
      + `and embeds all ${fmtNum(total)} tasks, learnings and memories again.`)) return
    run(async () => {
      await api.putEmbeddings({ model: wanted, trust_remote_code: wantedTrust })
      setSaved(true); setTimeout(() => setSaved(false), 2000)
      emb.refresh()
    })
  }

  return (
    <div className="card form">
      <h2>Search embeddings</h2>
      <p className="muted">Each job&rsquo;s prompt gets the learnings and the agent&rsquo;s memories that fit the job best, and
        the <code>recall</code> tool searches an agent&rsquo;s memories. DAASH finds them by meaning: this model turns every task,
        learning and memory into a vector, and PostgreSQL (pgvector) finds the nearest ones. <a href="#/help/memory">More in Help</a></p>
      <EmbedderStatus data={data} />
      <label><span>Model</span>
        <select value={choice || ''} onChange={(e) => setChoice(e.target.value)}>
          {catalog.map((m) => (
            <option key={m.name} value={m.name}>{m.label}{m.built_in ? ' (built in)' : ''}: {m.dim} dimensions, {sizeText(m)}</option>
          ))}
          <option value={OTHER}>Another Hugging Face model&hellip;</option>
        </select>
        {entry && <span className="muted field-help">{entry.note}{entry.name !== 'word-hashes' && <> <code>{entry.name}</code></>}</span>}
      </label>
      {choice === OTHER && (
        <>
          <label><span>Hugging Face model id</span>
            <input value={otherName} onChange={(e) => setOtherName(e.target.value)} maxLength={120} spellCheck={false}
              placeholder="org/name, e.g. intfloat/e5-base-v2" />
            <span className="muted field-help">A sentence-transformers embedding model.
              Gated models also need <code>HF_TOKEN</code> in <code>.env</code>.</span>
          </label>
          <label className="check"><input type="checkbox" checked={trust} onChange={(e) => setTrust(e.target.checked)} /> Let this model run its own code</label>
          <span className="muted field-help">Some models ship their own Python code (<code>trust_remote_code</code>). Tick this only for a
            model whose code you trust: it runs inside the embedder container.</span>
        </>
      )}
      <p className="muted field-help">Changing the model loads it (a model that is not built in is downloaded from Hugging Face first,
        which needs internet) and embeds every task, learning and memory again. Until then, items not embedded yet are found by
        recency.</p>
      <ErrorNote error={error} />
      <div className="row">
        <button className="btn primary" onClick={save} disabled={busy || !changed}>{busy ? <Spinner label="Saving" /> : 'Use this model'}</button>
        {saved && <span className="ok-text">Saved</span>}
      </div>
    </div>
  )
}
