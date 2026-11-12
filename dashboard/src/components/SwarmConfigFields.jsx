import React from 'react'
import {
  CONNECTED, HARNESS_CHOICES, HARNESS_NAMES, OPTIONAL_CAPS, capLabel, connectedStatus, defaultCaps, num, sameCaps, statusText,
} from '../util.js'

// The swarm configuration form, shared by Create a swarm (Advanced configuration) and
// Settings (Defaults for new swarms). `gatewayModels` comes from GET /models; `harnesses` and
// `connected` (connected agents) from GET /availability (all optional), to show what is really
// available.
const ROLES = [
  ['mission_judgement', 'Mission Judgement'],
  ['quality_judgement', 'Quality Judgement'],
  ['mission_delivery', 'Mission Delivery'],
  ['classifier', 'Classifier'],
]
const LIMITS = [
  ['max_total_tokens', 'Max tokens', 1],
  ['max_wall_clock_minutes', 'Max wall clock (min)', 1],
  ['max_simultaneous_agents', 'Simultaneous agents', 1],
  ['max_teams', 'Max teams', 1],
  ['max_tasks_per_team', 'Max tasks per team', 1],
  ['max_attempts_per_task', 'Attempts per task', 1],
]

function ModelSelect({ value, onChange, models }) {
  const list = models.includes(value) || !value ? models : [value, ...models]
  if (!list.length) return <input value={value || ''} onChange={(e) => onChange(e.target.value)} />
  return (
    <select value={value || ''} onChange={(e) => onChange(e.target.value)}>
      {list.map((m) => <option key={m} value={m}>{m}</option>)}
    </select>
  )
}

// Which models a swarm may use, in escalation order (model_ladder + allowed_models).
function ModelLadder({ cfg, setCfg, gatewayModels }) {
  const rows = [...new Set([...(cfg.model_ladder || []), ...gatewayModels, ...(cfg.allowed_models || [])])]
  const allowed = new Set(cfg.allowed_models || [])
  const save = (order, allow) => setCfg({ ...cfg, model_ladder: order, allowed_models: order.filter((m) => allow.has(m)) })
  const move = (i, d) => {
    const order = [...rows];
    [order[i], order[i + d]] = [order[i + d], order[i]]
    save(order, allowed)
  }
  const toggle = (m, on) => {
    const next = new Set(allowed)
    if (on) next.add(m); else next.delete(m)
    save(rows, next)
  }
  let step = 0
  return (
    <fieldset className="ladder">
      <legend>Models, in escalation order</legend>
      <ul className="ladder-list">
        {rows.map((m, i) => {
          const on = allowed.has(m)
          if (on) step += 1
          return (
            <li key={m} className={on ? '' : 'dim'}>
              <span className="ladder-step" aria-hidden="true">{on ? step : '-'}</span>
              <label className="inline"><input type="checkbox" checked={on} onChange={(e) => toggle(m, e.target.checked)} />
                <code>{m}</code></label>
              {gatewayModels.length > 0 && !gatewayModels.includes(m) && <span className="bad">not offered by the gateway</span>}
              <span className="ladder-move">
                <button type="button" className="btn small" disabled={i === 0} onClick={() => move(i, -1)}
                  aria-label={`Move ${m} earlier`}>↑</button>
                <button type="button" className="btn small" disabled={i === rows.length - 1} onClick={() => move(i, 1)}
                  aria-label={`Move ${m} later`}>↓</button>
              </span>
            </li>
          )
        })}
      </ul>
      <p className="muted">Cheapest first. Delivery work moves on to the next ticked model when it needs a stronger one.
        Unticked models are never used.</p>
    </fieldset>
  )
}

// A delivery row filled by a connected agent: what is wrong with it right now, if anything.
function connectedProblem(r, byId, loaded) {
  if (!r.connected_agent) return 'Pick a connected agent for this row.'
  const a = byId[r.connected_agent]
  if (!a) return loaded ? 'This connected agent was removed from DAASH. Pick another agent for this row, or remove the row.' : null
  if (!a.enabled) return `${a.name} is disabled: a swarm cannot be created with it until you enable it in Agents & models.`
  if (a.availability === 'available') return null
  const st = connectedStatus(a)
  return `${a.name} is ${st.text.toLowerCase()}: ${st.explain} Until it is available, its tasks wait, then go to DAASH’s built-in agent after the remote fallback time.`
}

// Harness mix (DESIGN-0.3 section 2, DESIGN-0.4 section 5.2): allowed harnesses + delivery rows.
// A row is an agent type, or a connected agent ({harness: 'connected', connected_agent: id},
// whose model is its own); picking one allows 'connected' automatically.
function HarnessMix({ cfg, setCfg, modelList, harnesses, connected }) {
  const allowed = cfg.allowed_harnesses || ['daash']
  const rows = cfg.delivery_harnesses || []
  const info = Object.fromEntries((harnesses || []).map((h) => [h.harness, h]))
  const agents = connected || []
  const byId = Object.fromEntries(agents.map((a) => [a.id, a]))
  const label = (h) => (info[h] && info[h].label) || HARNESS_NAMES[h] || h
  const toggle = (h, on) => {
    const next = on ? [...allowed, h] : allowed.filter((x) => x !== h)
    setCfg({ ...cfg, allowed_harnesses: next, delivery_harnesses: rows.filter((r) => next.includes(r.harness)) })
  }
  const setRow = (i, patch) => setCfg({ ...cfg, delivery_harnesses: rows.map((r, j) => (j === i ? { ...r, ...patch } : r)) })
  const choices = HARNESS_CHOICES.filter((h) => allowed.includes(h))
  const rowValue = (r) => (r.harness === CONNECTED ? `${CONNECTED}:${r.connected_agent || ''}` : r.harness)
  const pick = (i, value) => {
    if (value.startsWith(`${CONNECTED}:`)) {
      const id = value.slice(CONNECTED.length + 1)
      setCfg({
        ...cfg,
        allowed_harnesses: allowed.includes(CONNECTED) ? allowed : [...allowed, CONNECTED],
        delivery_harnesses: rows.map((r, j) => (j === i
          ? { ...r, harness: CONNECTED, connected_agent: id, model: '', capabilities: null } : r)),
      })
    } else {
      setRow(i, { harness: value, connected_agent: null, capabilities: null })
    }
  }
  const capsDefault = (r) => (r.harness === CONNECTED
    ? ((byId[r.connected_agent] && byId[r.connected_agent].capabilities) || ['edit_files']) : defaultCaps(r.harness))
  const note = (h) => {
    const s = info[h] && info[h].status
    return s && s !== 'available' ? <span className="bad"> ({statusText(s).toLowerCase()})</span> : null
  }
  const shownTypes = HARNESS_CHOICES.concat(allowed.includes(CONNECTED) || agents.length ? [CONNECTED] : [])
  const agentOption = (a) => `${a.label} · ${a.enabled ? connectedStatus(a).text.toLowerCase() : 'disabled'}`
  return (
    <>
      <fieldset className="harness-mix">
        <legend>Allowed agents</legend>
        <div className="row">
          {shownTypes.map((h) => (
            <label key={h} className="inline">
              <input type="checkbox" checked={allowed.includes(h)} onChange={(e) => toggle(h, e.target.checked)} /> {label(h)}{note(h)}
            </label>
          ))}
        </div>
        <p className="muted">OpenClaude, Claude Code, Codex, OpenClaw and Hermes Agent run on a runner; without one, their work
          falls back to the built-in agent after the fallback time below. Connected agents are agents that already run
          elsewhere (see <a href="#/agents/connected">Agents &amp; models</a>).</p>
      </fieldset>
      <fieldset className="harness-mix">
        <legend>Delivery agents</legend>
        {!rows.length && <div className="muted">No rows: every delivery agent uses the built-in daash agent.</div>}
        {rows.map((r, i) => {
          const isConnected = r.harness === CONNECTED
          const problem = isConnected ? connectedProblem(r, byId, connected !== undefined) : null
          const caps = capsDefault(r)
          const value = rowValue(r)
          const typeOptions = choices.includes(r.harness) || isConnected ? choices : [r.harness, ...choices]
          return (
            <div className="delivery-row" key={i}>
              <div className="row">
                <label>Agent for row {i + 1}
                  <select value={value} onChange={(e) => pick(i, e.target.value)}>
                    <optgroup label="Agent types">
                      {typeOptions.map((h) => <option key={h} value={h}>{label(h)}</option>)}
                    </optgroup>
                    {(agents.length > 0 || isConnected) && (
                      <optgroup label="Connected agents">
                        {isConnected && !byId[r.connected_agent] && (
                          <option value={value}>{r.connected_agent ? 'removed connected agent' : 'pick one'}</option>
                        )}
                        {agents.map((a) => (
                          <option key={a.id} value={`${CONNECTED}:${a.id}`} disabled={!a.enabled && r.connected_agent !== a.id}>
                            {agentOption(a)}</option>
                        ))}
                      </optgroup>
                    )}
                  </select></label>
                <label>Model
                  {isConnected ? (
                    <select value="" disabled title="DAASH does not choose a connected agent's model">
                      <option value="">its own model</option>
                    </select>
                  ) : (
                    <select value={r.model || ''} onChange={(e) => setRow(i, { model: e.target.value })}>
                      <option value="">escalation order (default)</option>
                      {(r.model && !modelList.includes(r.model) ? [r.model, ...modelList] : modelList)
                        .map((m) => <option key={m} value={m}>{m}</option>)}
                    </select>
                  )}</label>
                <fieldset className="caps-pick">
                  <legend>Capabilities (default: {caps.map(capLabel).join(', ')})</legend>
                  {OPTIONAL_CAPS.map((c) => {
                    const cur = r.capabilities || caps
                    return (
                      <label key={c} className="check"><input type="checkbox" checked={cur.includes(c)}
                        onChange={(e) => {
                          const next = e.target.checked ? [...cur, c] : cur.filter((x) => x !== c)
                          setRow(i, { capabilities: sameCaps(next, caps) ? null : next })
                        }} /> {capLabel(c)}</label>
                    )
                  })}
                </fieldset>
                <button type="button" className="btn small" aria-label={`Remove row ${i + 1}`}
                  onClick={() => setCfg({ ...cfg, delivery_harnesses: rows.filter((_, j) => j !== i) })}>Remove</button>
              </div>
              {isConnected && <p className="muted field-help">A connected agent uses its own model; DAASH does not choose it.</p>}
              {problem && <div className="warn-note">{problem}</div>}
            </div>
          )
        })}
        <button type="button" className="btn small" disabled={!choices.length && !agents.length}
          onClick={() => {
            const first = agents.find((a) => a.enabled)
            if (choices.length || !first) {
              setCfg({ ...cfg, delivery_harnesses: [...rows, { harness: choices[0] || 'daash', provider: 'default', model: '' }] })
            } else {
              setCfg({
                ...cfg,
                allowed_harnesses: allowed.includes(CONNECTED) ? allowed : [...allowed, CONNECTED],
                delivery_harnesses: [...rows, { harness: CONNECTED, connected_agent: first.id, provider: 'default', model: '' }],
              })
            }
          }}>
          + Add row</button>
        <p className="muted">Delivery agents cycle through these rows: agent 1 uses row 1, agent 2 row 2, and so on,
          wrapping around. {agents.length ? 'To use a connected agent, add a row and pick it under Connected agents.'
            : <>No connected agents yet: <a href="#/agents/connected">connect your own agents</a> to use them here.</>}</p>
      </fieldset>
      <div className="grid-2">
        <label>Remote fallback (minutes)
          <input type="number" min="1" value={cfg.remote_fallback_minutes ?? ''} placeholder="wait forever"
            onChange={(e) => setCfg({ ...cfg, remote_fallback_minutes: num(e.target.value) ?? null })} /></label>
        <label>Remote job timeout (minutes)
          <input type="number" min="1" value={cfg.limits.remote_job_timeout_minutes ?? 30}
            onChange={(e) => setCfg({ ...cfg, limits: { ...cfg.limits, remote_job_timeout_minutes: num(e.target.value) ?? 30 } })} /></label>
      </div>
    </>
  )
}

export default function SwarmConfigFields({ cfg, setCfg, gatewayModels = [], harnesses, connected }) {
  // Roles may only use allowed models, in escalation order.
  const allowedModels = (cfg.model_ladder || []).filter((m) => (cfg.allowed_models || []).includes(m))
    .concat((cfg.allowed_models || []).filter((m) => !(cfg.model_ladder || []).includes(m)))
  const setRole = (k, v) => setCfg({ ...cfg, role_models: { ...cfg.role_models, [k]: v } })
  const setLimit = (k, v) => setCfg({ ...cfg, limits: { ...cfg.limits, [k]: num(v) ?? null } })
  return (
    <div className="adv">
      <ModelLadder cfg={cfg} setCfg={setCfg} gatewayModels={gatewayModels} />
      <div className="grid-2">
        {ROLES.map(([k, label]) => (
          <label key={k}>{label} model
            <ModelSelect models={allowedModels} value={cfg.role_models[k]} onChange={(v) => setRole(k, v)} /></label>
        ))}
        <label>Coordinator model
          <ModelSelect models={allowedModels} value={cfg.coordinator.model}
            onChange={(v) => setCfg({ ...cfg, coordinator: { ...cfg.coordinator, model: v } })} /></label>
        <label>Delivery agents per team
          <input type="number" min="1" max="5" value={cfg.delivery_agents_per_team}
            onChange={(e) => setCfg({ ...cfg, delivery_agents_per_team: num(e.target.value) ?? 1 })} /></label>
        {LIMITS.map(([k, label, min]) => (
          <label key={k}>{label}
            <input type="number" min={min} value={cfg.limits[k] ?? ''} placeholder="unlimited"
              onChange={(e) => setLimit(k, e.target.value)} /></label>
        ))}
      </div>
      <HarnessMix cfg={cfg} setCfg={setCfg} modelList={allowedModels} harnesses={harnesses} connected={connected} />
    </div>
  )
}
