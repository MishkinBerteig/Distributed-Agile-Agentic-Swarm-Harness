import React, { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'
import { OPTIONAL_CAPS, capLabel, fmtDateTime, relTime } from '../util.js'
import { Collapsible, CopyBlock, ErrorNote, Spinner, useAction } from './Common.jsx'
import Modal from './Modal.jsx'

// Connecting an existing agent directly (docs/DESIGN-0.4.md section 5.8): a short form, then the
// owner's ONE approval step in two forms (a command, a message to the agent), then "Waiting for
// <name> to accept..." that turns into "Connected" by itself (DAASH notices the approval; this
// dialog polls the agent). The step's tokens are shown only here, once.

export const KIND_NAMES = { openclaw: 'OpenClaw', hermes: 'Hermes Agent', a2a: 'A2A agent', other: 'agent', unknown: 'agent' }
const POLL_MS = 2000

const suggestedName = (d) => (d && d.skills && d.skills.length === 1 ? d.skills[0] : '')

function CapsPicker({ caps, setCaps }) {
  return (
    <fieldset className="caps-pick">
      <legend>It can also</legend>
      {OPTIONAL_CAPS.map((c) => (
        <label key={c} className="check"><input type="checkbox" checked={caps.includes(c)}
          onChange={(e) => setCaps(e.target.checked ? [...caps, c] : caps.filter((x) => x !== c))} /> {capLabel(c)}</label>
      ))}
    </fieldset>
  )
}

function ConnectForm({ target, onConnected }) {
  const d = target.discovery
  const hermes = d ? d.kind === 'hermes' : target.protocol === 'hermes-api'
  const [name, setName] = useState(suggestedName(d))
  const [apiKey, setApiKey] = useState('')
  const [caps, setCaps] = useState(['edit_files'])
  const [maxJobs, setMaxJobs] = useState(1)
  const [keyError, setKeyError] = useState(null)
  const { busy, error, run } = useAction()

  const submit = (e) => {
    e.preventDefault()
    setKeyError(null)
    if (hermes && apiKey.trim() && apiKey.trim().length < 16) {
      setKeyError(new Error('Hermes needs an API key of 16 characters or more. Leave it empty and DAASH makes one.'))
      return
    }
    const payload = { capabilities: caps, max_jobs: maxJobs }
    if (name.trim()) payload.name = name.trim()
    if (d) payload.discovery_id = d.id
    else {
      payload.address = target.address
      if (target.protocol) payload.protocol = target.protocol
      if (target.kind) payload.kind = target.kind
    }
    if (hermes && apiKey.trim()) payload.api_key = apiKey.trim()
    run(async () => onConnected(await api.connectAgent(payload)))
  }

  const what = d
    ? <>{d.name || KIND_NAMES[d.kind] || 'An agent'} <span className="muted">({KIND_NAMES[d.kind] || d.kind} at {d.address}{d.skills && d.skills.length ? `; agents: ${d.skills.join(', ')}` : ''})</span></>
    : <><code>{target.address}</code>{target.protocol && <span className="muted"> as {hermes ? 'a Hermes Agent API server' : 'an OpenClaw gateway (A2A)'}</span>}</>

  return (
    <form className="form connect-form" onSubmit={submit}>
      <p>Connect {what}.</p>
      <p className="muted">DAASH then shows one step for the agent&rsquo;s owner. Nothing changes on the agent until its
        owner does that step.</p>
      <label><span>Name in DAASH <span className="muted">(what teams and the board call it)</span></span>
        <input value={name} onChange={(e) => setName(e.target.value)} maxLength={80}
          placeholder={d && d.name ? `e.g. ${suggestedName(d) || d.name}` : 'DAASH picks one from the agent'} /></label>
      {hermes && (
        <label><span>API key for its API server <span className="muted">(optional)</span></span>
          <input value={apiKey} onChange={(e) => setApiKey(e.target.value)} autoComplete="off" spellCheck={false}
            placeholder="Leave empty and DAASH makes a strong key" />
          <span className="muted field-help">If this Hermes already has an <code>API_SERVER_KEY</code>, paste it here (16 characters
            or more). Otherwise DAASH makes one and puts it in the owner&rsquo;s step.</span>
        </label>
      )}
      <CapsPicker caps={caps} setCaps={setCaps} />
      <p className="muted field-help">Every agent can edit files. Tasks that need code to run, or web research, go only to agents
        that can do it.</p>
      <label className="narrow">Tasks at once
        <input type="number" min="1" max="16" value={maxJobs}
          onChange={(e) => setMaxJobs(Math.min(16, Math.max(1, Number(e.target.value) || 1)))} /></label>
      <ErrorNote error={keyError || error} />
      <div className="row">
        <button className="btn primary" disabled={busy}>{busy ? <Spinner label="Connecting" /> : 'Connect'}</button>
      </div>
    </form>
  )
}

// What DAASH waits for, and what happened: shown above the step and updated by polling.
function WaitBanner({ agent, onRetry, retrying }) {
  if (agent.status === 'connected') {
    return (
      <div className="ok-banner" role="status">
        <strong>Connected &#10003;</strong> {agent.name} accepted DAASH&rsquo;s connection.
        <p>Use it in a swarm: in <a href="#/">Create a swarm</a> &rsaquo; Advanced configuration (or <a href="#/settings">Settings</a> &rsaquo;
          Defaults for new swarms), add a delivery agent and pick <strong>{agent.label}</strong>.</p>
      </div>
    )
  }
  if (agent.status !== 'pending') {
    return <div className="warn-note" role="status">{agent.name} is {agent.status}.</div>
  }
  if (agent.pending_expired) {
    return (
      <div className="warn-note" role="status">
        <strong>DAASH stopped waiting</strong> at {fmtDateTime(agent.pending_until)}: {agent.name} did not accept within the hour.
        {agent.last_error && <div className="muted">Last check: {agent.last_error}</div>}
        <p>If its owner has done the step by now (or will soon), Try again waits another hour. The step below still works.</p>
        <button type="button" className="btn small primary" onClick={onRetry} disabled={retrying}>
          {retrying ? <Spinner label="Starting" /> : 'Try again'}</button>
      </div>
    )
  }
  return (
    <div className="wait-banner" role="status">
      <div><Spinner /> <strong>Waiting for {agent.name} to accept&hellip;</strong></div>
      <p>DAASH checks every few seconds, until {fmtDateTime(agent.pending_until)} ({relTime(agent.pending_until)}).
        This updates by itself when the owner has done the step below: nothing else to click.</p>
      {agent.last_error && <p className="muted">Last check: {agent.last_error}{agent.error_kind === 'token_rejected'
        ? ' (expected until the step is done)' : ''}</p>}
    </div>
  )
}

function ApprovalStep({ agent, step }) {
  const isHermes = step.protocol === 'hermes-api'
  const isCommand = isHermes || step.kind === 'openclaw'
  return (
    <div className="approval">
      <h3>The owner&rsquo;s one step</h3>
      <p>Do this once, {step.where || 'on the agent’s machine'}. Use <strong>either</strong> form.</p>
      {(step.warnings || []).map((w) => <div key={w} className="warn-note">{w}</div>)}
      <CopyBlock title={isCommand ? 'A. Paste this command in a terminal there' : 'A. Set this in the agent’s own settings'}
        text={step.command} copyLabel={isCommand ? 'Copy command' : 'Copy'} />
      {step.restart && !String(step.command || '').includes(step.restart) && (
        <p>Then run <code>{step.restart}</code>.</p>
      )}
      <CopyBlock title={`B. Or send this message to ${agent.name}`} text={step.message} copyLabel="Copy message">
        <p className="muted field-help">In your usual chat with {agent.name}. This works where {agent.name} may change its own
          configuration for you; it then does the same change itself.</p>
      </CopyBlock>
      {step.note && <p className="muted">{step.note}</p>}
      {isHermes && (
        <Collapsible title="The same settings by hand (.env lines and config.yaml)">
          {step.api_key && <CopyBlock title="The API key DAASH will use" text={step.api_key} copyLabel="Copy key" />}
          {step.env && <CopyBlock title=".env lines of its profile" text={step.env} />}
          {step.mcp_yaml && <CopyBlock title="config.yaml: DAASH's tools" text={step.mcp_yaml} />}
        </Collapsible>
      )}
      <div className="secret-note">
        <strong>Shown only now.</strong> This step contains new secret tokens. DAASH cannot show them again: if they get
        lost, use <em>Reset tokens</em> on the agent to make new ones (the old ones then stop working).
      </div>
      <p className="muted">What the step does, in detail: <a href={`#/help/connect/${isHermes ? 'approval-hermes' : step.kind === 'openclaw' ? 'approval-openclaw' : 'approval-a2a'}`}>Help &rsaquo; The one approval step</a>.</p>
    </div>
  )
}

// `target`: {discovery} | {address, protocol?, kind?} (shows the form first), or {result} (the
// response of connect or reset-tokens: goes straight to the step).
export default function ConnectDialog({ target, onClose, onChanged }) {
  const [result, setResult] = useState(target.result || null)
  const [agent, setAgent] = useState(target.result ? target.result.agent : null)
  const [gone, setGone] = useState(false)
  const retry = useAction()

  const connected = (r) => { setResult(r); setAgent(r.agent); onChanged && onChanged() }

  const waiting = agent && agent.status === 'pending' && !agent.pending_expired
  const refresh = useCallback(async () => {
    if (!agent) return
    try {
      const a = await api.getConnected(agent.id)
      setAgent((old) => {
        if (old && old.status !== a.status && onChanged) onChanged()
        return a
      })
    } catch (e) {
      if (e.status === 404) setGone(true)
    }
  }, [agent && agent.id, onChanged]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!waiting || gone) return undefined
    const t = setInterval(refresh, POLL_MS)
    return () => clearInterval(t)
  }, [waiting, gone, refresh])

  const close = () => {
    if (agent && agent.status === 'pending' && !gone && !window.confirm(
      `Close the approval step for ${agent.name}? Its tokens are not shown again. DAASH keeps waiting in the background; `
      + 'if you need the step again, use Reset tokens on the agent.')) return
    onClose()
  }

  const name = agent ? agent.name : (target.discovery && (suggestedName(target.discovery) || target.discovery.name)) || target.address || 'an agent'
  const title = !agent ? `Connect ${name}`
    : agent.status === 'connected' ? `${agent.name} is connected ✓`
      : agent.pending_expired ? `${agent.name}: not approved yet` : `Connecting ${agent.name}: waiting for approval`

  return (
    <Modal title={title} onClose={close} wide>
      {!result ? <ConnectForm target={target} onConnected={connected} /> : (
        <>
          {gone ? <div className="error-note">{agent.name} was removed from DAASH.</div>
            : <WaitBanner agent={agent} retrying={retry.busy}
              onRetry={() => retry.run(async () => setAgent(await api.retryConnected(agent.id)))} />}
          <ErrorNote error={retry.error} />
          {agent.status === 'connected' ? (
            <Collapsible title="The approval step again (already done)">
              <ApprovalStep agent={agent} step={result.approval} />
            </Collapsible>
          ) : <ApprovalStep agent={agent} step={result.approval} />}
          <div className="row dialog-foot">
            <button type="button" className="btn" onClick={close}>{agent.status === 'connected' ? 'Done' : 'Close'}</button>
            {agent.status === 'pending' && <span className="muted">DAASH keeps waiting after you close this.</span>}
          </div>
        </>
      )}
    </Modal>
  )
}
