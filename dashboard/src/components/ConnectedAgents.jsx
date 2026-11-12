import React, { useState } from 'react'
import { api } from '../api.js'
import { OPTIONAL_CAPS, ago, capLabel, connectedStatus, connectedWhere, fmtDateTime, relTime } from '../util.js'
import { Empty, ErrorNote, Spinner, useAction } from './Common.jsx'

// Agents & models > Connected agents (docs/DESIGN-0.4.md sections 5.7 and 7): agents that already
// exist elsewhere and work in DAASH's swarms. Each shows where it runs, its status in plain
// words, what it can do, whether it really uses DAASH's tools, and its last error, with Test,
// Enable/Disable, Edit, Reset tokens and Remove.

export function StatusChip({ agent }) {
  const st = connectedStatus(agent)
  return <span className={`chip ca-status t-${st.tone}`}>{st.text}</span>
}

function EditForm({ a, onDone }) {
  const [name, setName] = useState(a.name)
  const [caps, setCaps] = useState(a.capabilities || ['edit_files'])
  const [maxJobs, setMaxJobs] = useState(a.max_jobs || 1)
  const [endpoint, setEndpoint] = useState(a.endpoint || '')
  const [apiKey, setApiKey] = useState('')
  const { busy, error, run } = useAction()
  const save = (e) => {
    e.preventDefault()
    const patch = {}
    if (name.trim() && name.trim() !== a.name) patch.name = name.trim()
    if ([...caps].sort().join() !== [...(a.capabilities || [])].sort().join()) patch.capabilities = caps
    if (maxJobs !== a.max_jobs) patch.max_jobs = maxJobs
    if (a.protocol === 'a2a' && endpoint.trim() && endpoint.trim() !== (a.endpoint || '')) patch.endpoint = endpoint.trim()
    if (a.protocol === 'hermes-api' && apiKey.trim()) patch.api_key = apiKey.trim()
    if (!Object.keys(patch).length) { onDone(false); return }
    run(async () => { await api.patchConnected(a.id, patch); onDone(true) })
  }
  return (
    <form className="form ca-edit" onSubmit={save}>
      <label>Name<input value={name} onChange={(e) => setName(e.target.value)} maxLength={80} required /></label>
      <fieldset className="caps-pick">
        <legend>Can</legend>
        <label className="check"><input type="checkbox" checked disabled /> {capLabel('edit_files')}</label>
        {OPTIONAL_CAPS.map((c) => (
          <label key={c} className="check"><input type="checkbox" checked={caps.includes(c)}
            onChange={(e) => setCaps(e.target.checked ? [...caps, c] : caps.filter((x) => x !== c))} /> {capLabel(c)}</label>
        ))}
      </fieldset>
      <label className="narrow">Tasks at once
        <input type="number" min="1" max="16" value={maxJobs}
          onChange={(e) => setMaxJobs(Math.min(16, Math.max(1, Number(e.target.value) || 1)))} /></label>
      {a.protocol === 'a2a' && (
        <label><span>A2A endpoint <span className="muted">(only if DAASH must use another address)</span></span>
          <input value={endpoint} onChange={(e) => setEndpoint(e.target.value)} spellCheck={false} /></label>
      )}
      {a.protocol === 'hermes-api' && (
        <label><span>New API key <span className="muted">(only if its owner changed API_SERVER_KEY)</span></span>
          <input value={apiKey} onChange={(e) => setApiKey(e.target.value)} autoComplete="off" spellCheck={false}
            minLength={8} placeholder="unchanged" /></label>
      )}
      <p className="muted field-help">New capabilities apply to teams created afterwards.</p>
      <ErrorNote error={error} />
      <div className="row">
        <button className="btn primary small" disabled={busy}>{busy ? <Spinner label="Saving" /> : 'Save'}</button>
        <button type="button" className="btn small" onClick={() => onDone(false)}>Cancel</button>
      </div>
    </form>
  )
}

function Usage({ usage, scope }) {
  if (!usage) return <span className="muted">-</span>
  const where = scope ? 'this swarm' : 'new swarms'
  if (usage.delivery_rows) {
    return <>{usage.delivery_rows} delivery row{usage.delivery_rows > 1 ? 's' : ''} of {where}
      {!usage.allowed && <span className="bad"> (not allowed there now)</span>}</>
  }
  return <span className="muted">Not in any delivery row of {where} yet{scope ? '' : <> (<a href="#/settings">Settings</a>, or
    Create a swarm &rsaquo; Advanced configuration)</>}</span>
}

function AgentCard({ a, usage, scope, onChanged, onShowStep }) {
  const st = connectedStatus(a)
  const [editing, setEditing] = useState(false)
  const [test, setTest] = useState(null)
  const [testing, setTesting] = useState(false)
  const act = useAction()

  const doTest = () => act.run(async () => {
    setTest(null)
    setTesting(true)
    try {
      setTest(await api.checkConnected(a.id))
    } finally { setTesting(false) }
    onChanged()
  })
  const toggle = () => act.run(async () => { await api.patchConnected(a.id, { enabled: !a.enabled }); onChanged() })
  const retry = () => act.run(async () => { await api.retryConnected(a.id); onChanged() })
  const reset = () => {
    if (!window.confirm(`Make new tokens for ${a.name}? The current ones stop working at once, its DAASH tasks in progress `
      + 'end, and it waits for approval again until its owner does the new step.')) return
    act.run(async () => { onShowStep(await api.resetConnectedTokens(a.id)); onChanged() })
  }
  const remove = () => {
    if (!window.confirm(`Remove ${a.name} from DAASH? DAASH stops giving it work and its DAASH tools stop working. `
      + 'Delivery rows that name it must be changed before a new swarm can use them.'
      + (a.direct ? ' On the agent’s side, its owner can remove the "daash" peer and MCP server.' : ''))) return
    act.run(async () => { await api.deleteConnected(a.id); onChanged() })
  }

  return (
    <li className={`ca-card${a.enabled ? '' : ' dim'}`}>
      <div className="ca-head">
        <span className="ca-name"><span className={`dot ${st.tone}`} aria-hidden="true" /><strong>{a.name}</strong></span>
        <StatusChip agent={a} />
      </div>
      <div className="ca-meta">
        {a.kind_label} &middot; {a.direct ? <>direct, {a.protocol_label}</> : 'through an agent host'} &middot; {connectedWhere(a)}
        {a.runner && <span className="muted"> ({a.runner.online ? 'online' : 'offline'})</span>}
      </div>
      <p className="ca-explain muted">{st.explain}
        {a.status === 'pending' && !a.pending_expired && a.pending_until && <> DAASH waits until {fmtDateTime(a.pending_until)} ({relTime(a.pending_until)}).
          Lost the step? Reset tokens makes a new one.</>}</p>
      {a.pending_expired && (
        <div className="warn-note">DAASH stopped waiting for approval at {fmtDateTime(a.pending_until)}.{' '}
          <button type="button" className="btn small" onClick={retry} disabled={act.busy}>Try again</button>
          <span className="muted"> waits another hour.</span></div>
      )}
      <dl className="ca-facts">
        <dt>Can</dt>
        <dd><span className="caps">{(a.capabilities || []).map((c) => <span key={c} className="chip cap-chip">{capLabel(c)}</span>)}</span>
          <span className="muted"> &middot; {a.max_jobs} task{a.max_jobs > 1 ? 's' : ''} at once</span></dd>
        <dt>DAASH tools</dt>
        <dd>{a.daash_tools && a.daash_tools.in_use
          ? <>in use <span className="muted">(last call {ago(a.daash_tools.last_call)})</span></>
          : <span className="muted">not yet used (an agent calls them while it works on a DAASH task)</span>}</dd>
        <dt>{scope ? 'This swarm' : 'New swarms'}</dt>
        <dd><Usage usage={usage} scope={scope} /></dd>
        {a.last_ok_at && <><dt>Last check passed</dt><dd>{ago(a.last_ok_at)}</dd></>}
        {a.last_error && <><dt>Last error</dt><dd className="bad ca-error">{a.last_error}</dd></>}
      </dl>
      {test && (
        <div className={test.ok ? 'ok-note' : 'error-note'} role="status">
          {test.ok ? `Test passed: ${a.name} answered${a.direct ? ' and accepted DAASH’s token' : ''}.`
            : `Test failed: ${test.error || test.error_kind || 'no answer'}`}
        </div>
      )}
      <ErrorNote error={act.error} />
      <div className="actions ca-actions">
        <button type="button" className="btn small" onClick={doTest} disabled={act.busy}>
          {testing ? <Spinner label="Testing" /> : 'Test'}</button>
        <button type="button" className="btn small" onClick={toggle} disabled={act.busy}>{a.enabled ? 'Disable' : 'Enable'}</button>
        <button type="button" className="btn small" onClick={() => setEditing(!editing)} aria-expanded={editing}>Edit</button>
        {a.direct && <button type="button" className="btn small" onClick={reset} disabled={act.busy}>Reset tokens</button>}
        <button type="button" className="btn small danger" onClick={remove} disabled={act.busy}>Remove</button>
      </div>
      {!a.direct && <p className="muted field-help">Its tokens come from its agent host, so it has no Reset tokens.</p>}
      {editing && <EditForm a={a} onDone={(changed) => { setEditing(false); if (changed) onChanged() }} />}
    </li>
  )
}

export default function ConnectedAgents({ list, usage, scope, endpoint, onChanged, onShowStep, onLookAround }) {
  const agents = (list.data && list.data.connected_agents) || []
  const ep = endpoint || (list.data && list.data.agent_endpoint)
  return (
    <section className="card" id="agents-connected" aria-labelledby="connected-h">
      <div className="head-top">
        <h2 id="connected-h">Connected agents</h2>
        <a href="#/help/connect" className="help-link">Help: connecting agents</a>
      </div>
      <p className="muted">Agents that already exist and run elsewhere, such as your own OpenClaw or Hermes agents, with their own
        model, memory and tools. In a swarm they fill delivery rows like any other agent. Connect one from{' '}
        <button type="button" className="link" onClick={() => onLookAround('found')}>Found on your network</button>, or{' '}
        <button type="button" className="link" onClick={() => onLookAround('hosts')}>on another machine</button> behind a firewall.</p>
      {ep && !ep.configured && (
        <div className="warn-note"><strong>Agents on other machines cannot reach DAASH&rsquo;s tools yet:</strong>{' '}
          <code>DAASH_AGENT_PUBLIC_URL</code> is not set, so approval steps contain a placeholder address. {ep.how_to_set}</div>
      )}
      {list.error && list.data === undefined && <ErrorNote error={list.error} />}
      {list.loading && list.data === undefined ? <Spinner label="Loading connected agents" /> : !agents.length ? (
        <Empty>No connected agents yet. Pick one under Found on your network, or add one by address.</Empty>
      ) : (
        <ul className="ca-list">
          {agents.map((a) => (
            <AgentCard key={a.id} a={a} usage={usage[a.id]} scope={scope} onChanged={onChanged} onShowStep={onShowStep} />
          ))}
        </ul>
      )}
    </section>
  )
}
