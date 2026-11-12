import React, { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { ago } from '../util.js'
import { Empty, ErrorNote, Spinner, useAction } from './Common.jsx'
import { KIND_NAMES } from './ConnectDialog.jsx'

// Agents & models > Found on your network (docs/DESIGN-0.4.md section 5.8): what discovery found,
// each with Connect; ports that listen but never answer point to agent hosts. Look again runs
// discovery now; while this page is open it runs every 10 minutes. Add by address takes a bare
// host name (DAASH tries the usual ports), host:port or a URL.

const EVERY_MS = 10 * 60 * 1000
const FOUND_LABEL = { openclaw: 'OpenClaw gateway', hermes: 'Hermes Agent API', a2a: 'A2A agent', other: 'agent' }

function FoundItem({ d, onConnect, onUseHost }) {
  if (d.status === 'silent') {
    return (
      <li className="found-item silent">
        <div className="found-head"><strong>{d.host}</strong><span className="chip">port {d.port}: no answer</span></div>
        <p>Something is listening at <code>{d.address}</code> but doesn&rsquo;t answer: probably a firewall. Connect the agents on
          that machine through an agent host instead.</p>
        <div className="row">
          <button type="button" className="btn small" onClick={onUseHost}>Use an agent host</button>
          <span className="muted">last seen {ago(d.last_seen_at)}</span>
        </div>
      </li>
    )
  }
  return (
    <li className="found-item">
      <div className="found-head">
        <strong>{d.name || d.host}</strong>
        <span className="chip">{FOUND_LABEL[d.kind] || KIND_NAMES[d.kind] || d.kind}</span>
        {d.connected && <span className="chip t-on">connected as {d.connected_agent_name || 'an agent'}</span>}
      </div>
      <div className="muted">{d.address}{d.detail ? ` · ${d.detail}` : ''}</div>
      <div className="row">
        {!d.connected && <button type="button" className="btn small primary" onClick={() => onConnect({ discovery: d })}>Connect</button>}
        {d.connected && <span className="muted">See Connected agents above.</span>}
        <span className="muted">last seen {ago(d.last_seen_at)}</span>
      </div>
    </li>
  )
}

function Summary({ run }) {
  const l = run.looked_at || {}
  const hosts = l.hosts || []
  return (
    <div className="discover-summary" role="status">
      <p className="muted">Looked at {l.host_count ?? hosts.length} machine{(l.host_count ?? hosts.length) === 1 ? '' : 's'}
        {hosts.length > 0 && hosts.length <= 6 && <> ({hosts.join(', ')})</>} on port{(l.ports || []).length === 1 ? '' : 's'}{' '}
        {(l.ports || []).join(', ')} in {run.seconds}s: {run.found} found, {run.silent} not answering.</p>
      {(l.problems || []).map((p) => <div key={p} className="warn-note">{p}</div>)}
      {(run.hints || []).map((h) => <div key={h} className="info-note">{h}</div>)}
    </div>
  )
}

function AddByAddress({ onConnect, onProbed }) {
  const [address, setAddress] = useState('')
  const [kind, setKind] = useState('auto')
  const [note, setNote] = useState(null)
  const { busy, error, run } = useAction()
  const submit = (e) => {
    e.preventDefault()
    setNote(null)
    const addr = address.trim()
    if (!addr) return
    if (kind === 'openclaw') { onConnect({ address: addr, protocol: 'a2a', kind: 'openclaw' }); return }
    if (kind === 'hermes') { onConnect({ address: addr, protocol: 'hermes-api', kind: 'hermes' }); return }
    run(async () => {
      const r = await api.probeAddress(addr)
      onProbed()
      const found = (r.found || []).filter((d) => d.status === 'found')
      const fresh = found.filter((d) => !d.connected)
      if (fresh.length === 1) { onConnect({ discovery: fresh[0] }); setAddress(''); return }
      if (fresh.length > 1) { setNote({ ok: true, text: `Found ${fresh.length} agents at ${addr}: they are in the list below; pick one to connect.` }); return }
      if (found.length) { setNote({ ok: true, text: `${found.map((d) => d.connected_agent_name || d.name).join(', ')} at ${addr} is already connected.` }); return }
      if ((r.found || []).some((d) => d.status === 'silent')) {
        setNote({ ok: false, text: `Something is listening at ${addr} but doesn’t answer: probably a firewall. Connect it through an agent host instead.` })
        return
      }
      setNote({ ok: false, text: r.message || `Nothing answered at ${addr}.` })
    })
  }
  return (
    <form className="form add-address" onSubmit={submit}>
      <h3>Add by address</h3>
      <div className="add-row">
        <label className="grow">Address
          <input value={address} onChange={(e) => setAddress(e.target.value)} spellCheck={false} autoCapitalize="off"
            placeholder="e.g. jane-mac or jane-mac.tailnet.ts.net:18789" /></label>
        <label>What is it?
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="auto">Find out by itself</option>
            <option value="openclaw">OpenClaw (not answering yet)</option>
            <option value="hermes">Hermes Agent (API server not on yet)</option>
          </select></label>
        <button className="btn small primary" disabled={busy || !address.trim()}>
          {busy ? <Spinner label="Looking" /> : kind === 'auto' ? 'Look' : 'Connect'}</button>
      </div>
      <p className="muted field-help">A host name alone is tried on the usual ports (18789 for OpenClaw, 8642 for Hermes). Also
        accepted: <code>host:port</code> or the full URL of an Agent Card. A Hermes Agent does not answer until its owner
        turns its API server on, so choose Hermes Agent for one.</p>
      <ErrorNote error={error} />
      {note && <div className={note.ok ? 'info-note' : 'warn-note'} role="status">{note.text}</div>}
    </form>
  )
}

export default function Discoveries({ onConnect, onUseHost, reloadSignal }) {
  const [list, setList] = useState(null)
  const [loadError, setLoadError] = useState(null)
  const [last, setLast] = useState(null)
  const look = useAction()
  const busyRef = useRef(false)

  const load = useCallback(async () => {
    try { const r = await api.listDiscoveries(); setList(r.discoveries || []); setLoadError(null); return r.discoveries || [] }
    catch (e) { setLoadError(e); return null }
  }, [])

  const discover = useCallback(async () => {
    if (busyRef.current) return
    busyRef.current = true
    await look.run(async () => {
      const r = await api.discover()
      setLast(r)
      setList(r.discoveries || [])
    })
    busyRef.current = false
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // On open: show what is stored, and look again when nothing was seen in the last 10 minutes.
  useEffect(() => {
    let stopped = false
    load().then((rows) => {
      if (stopped || rows === null) return
      const newest = Math.max(0, ...rows.map((d) => new Date(d.last_seen_at).getTime() || 0))
      if (Date.now() - newest > EVERY_MS) discover()
    })
    const t = setInterval(() => { if (!document.hidden) discover() }, EVERY_MS)
    return () => { stopped = true; clearInterval(t) }
  }, [load, discover])
  useEffect(() => { if (reloadSignal) load() }, [reloadSignal, load])

  const found = (list || []).filter((d) => d.status === 'found')
  const silent = (list || []).filter((d) => d.status === 'silent')
  return (
    <section className="card" id="agents-found" aria-labelledby="found-h">
      <div className="head-top">
        <h2 id="found-h">Found on your network</h2>
        <button type="button" className="btn small" onClick={discover} disabled={look.busy}>
          {look.busy ? <Spinner label="Looking" /> : 'Look again'}</button>
      </div>
      <p className="muted">OpenClaw gateways, Hermes Agent API servers and other A2A agents DAASH found on the machines it looks at
        (<code>DAASH_DISCOVERY_HOSTS</code>, your runners&rsquo; machines and <code>DAASH_DISCOVERY_SUBNETS</code>). It never sends a
        password or token while it looks, and looks again every 10 minutes while this page is open.{' '}
        <a href="#/help/connect/discovery">How discovery works</a></p>
      <ErrorNote error={look.error} />
      {last && <Summary run={last} />}
      {loadError && list === null && <ErrorNote error={loadError} />}
      {list === null && !loadError ? <Spinner label="Loading" /> : list && !list.length ? (
        <Empty>{look.busy ? 'Looking…' : 'Nothing found yet. Look again, or add an agent by address below.'}</Empty>
      ) : list && (
        <ul className="found-list">
          {found.map((d) => <FoundItem key={d.id} d={d} onConnect={onConnect} onUseHost={onUseHost} />)}
          {silent.map((d) => <FoundItem key={d.id} d={d} onConnect={onConnect} onUseHost={onUseHost} />)}
        </ul>
      )}
      <AddByAddress onConnect={onConnect} onProbed={load} />
    </section>
  )
}
