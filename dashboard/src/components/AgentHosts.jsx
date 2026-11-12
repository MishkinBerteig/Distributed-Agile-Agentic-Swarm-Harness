import React, { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'
import { connectedStatus, fmtDateTime, relTime } from '../util.js'
import { CopyBlock, ErrorNote, Spinner, useAction } from './Common.jsx'
import { StatusChip } from './ConnectedAgents.jsx'

// Agents & models > Connect agents on another machine (docs/DESIGN-0.4.md section 5.9): for agents
// nothing can connect to (a firewall that must not change). The owner runs ONE command there; it
// installs a DAASH runner that dials out, finds the machine's OpenClaw and Hermes agents and adds
// them here, already approved. Works against POST/GET /runner-enrollments; says so plainly when
// this coordinator does not have them.

const POLL_MS = 3000

function NewCode({ code, onDone }) {
  const [state, setState] = useState(null)       // GET /runner-enrollments/{id}
  const [stateMissing, setStateMissing] = useState(false)
  const cancel = useAction()

  const poll = useCallback(async () => {
    try { setState(await api.getEnrollment(code.id)) } catch (e) { if (e.status === 404 || e.status === 405) setStateMissing(true) }
  }, [code.id])
  const status = (state && state.status) || code.status || 'pending'
  useEffect(() => {
    if (stateMissing || !code.id || status !== 'pending') return undefined
    const t = setInterval(poll, POLL_MS)
    return () => clearInterval(t)
  }, [poll, stateMissing, code.id, status])
  // keep watching the new runner's agents for a while after the code was used
  useEffect(() => {
    if (status !== 'used' || stateMissing) return undefined
    const t = setInterval(poll, POLL_MS * 2)
    return () => clearInterval(t)
  }, [poll, status, stateMissing])

  const revoke = () => cancel.run(async () => { await api.deleteEnrollment(code.id); onDone() })
  const runner = state && state.runner
  const agents = (state && state.agents) || []
  const kindName = (k) => (k === 'hermes' ? 'Hermes Agent' : k === 'openclaw' ? 'OpenClaw' : k)
  return (
    <div className="host-code">
      {status !== 'used' && <>
      <CopyBlock title="Run this in a terminal on the agents' machine, as their owner" text={code.command} copyLabel="Copy command" />
      <p className="muted">Works once, until {fmtDateTime(code.expires_at)} ({relTime(code.expires_at)}). Needs <code>python3</code> 3.9 or
        newer and <code>curl</code> on that machine (macOS or Linux); no admin rights, and nothing starts listening there. Later,{' '}
        <code>python3 ~/daash-runner.pyz status</code> shows what is connected and <code>python3 ~/daash-runner.pyz disconnect</code>{' '}
        undoes it all.</p>
      </>}
      {status === 'pending' && (
        <div className="wait-banner" role="status">
          <div><Spinner /> <strong>Waiting for the machine to run this command&hellip;</strong></div>
          <p>This updates by itself. The agents it finds then appear here and under Connected agents, already approved.</p>
          <button type="button" className="btn small" onClick={revoke} disabled={cancel.busy}>Cancel this code</button>
        </div>
      )}
      {status === 'expired' && (
        <div className="warn-note">This code expired unused. <button type="button" className="btn small" onClick={onDone}>Make a new one</button></div>
      )}
      {status === 'used' && (
        <div className="ok-banner" role="status">
          <strong>Connected &#10003;</strong> {runner ? (runner.host || runner.name) : 'A machine used this code'}
          {agents.length ? <> &mdash; found {agents.map((a) => `${a.name} (${kindName(a.kind)})`).join(', ')}.</> : '.'}
          {runner && <p>Its DAASH runner <strong>{runner.name}</strong> is {runner.online ? 'online' : 'offline'}. The agents are already
            approved: pick them in a swarm&rsquo;s delivery rows like any connected agent.</p>}
          {agents.length ? (
            <ul className="host-agents">
              {agents.map((a) => (
                <li key={a.id}><strong>{a.name}</strong> <span className="muted">{kindName(a.kind)}
                  {a.detail ? ` · ${a.detail}` : ''}</span> <span className={`chip ca-status t-${a.status === 'connected' ? 'on' : 'off'}`}>{a.status}</span></li>
              ))}
            </ul>
          ) : <p>It has not reported any agents yet; they appear here within a few minutes.</p>}
          <button type="button" className="btn small" onClick={onDone}>Done</button>
        </div>
      )}
      <ErrorNote error={cancel.error} />
    </div>
  )
}

export default function AgentHosts({ connected, endpoint }) {
  const [unsupported, setUnsupported] = useState(false)
  const [pending, setPending] = useState([])
  const [code, setCode] = useState(null)
  const make = useAction()
  const revoke = useAction()

  const loadPending = useCallback(async () => {
    try {
      const r = await api.listEnrollments()
      const rows = (r && (r.enrollments || r.pending)) || (Array.isArray(r) ? r : [])
      setPending(rows.filter((e) => (e.status || 'pending') === 'pending'))
      setUnsupported(false)
    } catch (e) {
      if (e.status === 404 || e.status === 405) setUnsupported(true)
    }
  }, [])
  useEffect(() => { loadPending() }, [loadPending])

  const [notReady, setNotReady] = useState(null)   // 409: DAASH_AGENT_PUBLIC_URL is not set
  const newCode = () => make.run(async () => {
    setNotReady(null)
    try {
      setCode(await api.createEnrollment())
      loadPending()
    } catch (e) {
      if (e.status === 404 || e.status === 405) { setUnsupported(true); return }
      if (e.status === 409) { setNotReady(e.message); return }
      throw e
    }
  })
  const revokeOne = (id) => revoke.run(async () => { await api.deleteEnrollment(id); loadPending() })

  const hosted = (connected || []).filter((a) => a.protocol === 'runner')
  const otherPending = pending.filter((e) => !code || e.id !== code.id)

  return (
    <section className="card" id="agents-hosts" aria-labelledby="hosts-h">
      <div className="head-top">
        <h2 id="hosts-h">Connect agents on another machine</h2>
        <a href="#/help/connect/agent-host" className="help-link">Help: agent hosts</a>
      </div>
      <p>For agents on a machine that nothing can connect to (behind a firewall), run <strong>one command</strong> there. It installs a
        small DAASH runner that connects out to DAASH, finds the OpenClaw and Hermes agents on that machine, gives them DAASH&rsquo;s
        tools, and adds them here, already approved.</p>
      {endpoint && !endpoint.configured && (
        <div className="warn-note"><code>DAASH_AGENT_PUBLIC_URL</code> is not set, so the command cannot tell the other machine where
          DAASH is. {endpoint.how_to_set}</div>
      )}
      {unsupported ? (
        <div className="warn-note" role="status"><strong>Not available in this DAASH yet.</strong> This coordinator cannot make agent-host
          commands (it has no <code>/runner-enrollments</code> endpoint). Update DAASH and restart it, then come back here. Meanwhile, a
          runner can still be added by hand under Runners below.</div>
      ) : code ? (
        <NewCode code={code} onDone={() => { setCode(null); loadPending() }} />
      ) : (
        <div className="row">
          <button type="button" className="btn primary" onClick={newCode} disabled={make.busy}>
            {make.busy ? <Spinner label="Making a one-time code" /> : 'Show the install command'}</button>
          <span className="muted">Makes a one-time code, valid for an hour.</span>
        </div>
      )}
      {notReady && (
        <div className="warn-note" role="alert"><strong>DAASH cannot make the command yet.</strong> {notReady}{' '}
          <a href="#/help/connect/network">Help: Networking</a></div>
      )}
      <ErrorNote error={make.error || revoke.error} />
      {otherPending.length > 0 && (
        <div className="host-pending">
          <h3>Unused codes</h3>
          <ul>
            {otherPending.map((e) => (
              <li key={e.id}>A code made {fmtDateTime(e.created_at)}, expires {relTime(e.expires_at)}.{' '}
                <button type="button" className="btn small" onClick={() => revokeOne(e.id)} disabled={revoke.busy}>Revoke</button></li>
            ))}
          </ul>
          <p className="muted field-help">Codes are shown only once. If you lost one, revoke it and make a new one.</p>
        </div>
      )}
      <h3>Agents on agent hosts</h3>
      {!hosted.length ? <p className="muted">None yet. Agents the runner finds appear here and under Connected agents.</p> : (
        <ul className="host-agents">
          {hosted.map((a) => (
            <li key={a.id}><strong>{a.name}</strong> <span className="muted">{a.kind_label} on {a.runner ? a.runner.name : 'an agent host'}
              {a.runner ? ` (${a.runner.online ? 'online' : 'offline'})` : ''}</span> <StatusChip agent={a} />
              {a.availability !== 'available' && <div className="muted field-help">{connectedStatus(a).explain}</div>}</li>
          ))}
        </ul>
      )}
    </section>
  )
}
