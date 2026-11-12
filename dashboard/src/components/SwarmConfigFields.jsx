import React from 'react'
import { HARNESS_CHOICES, OPTIONAL_CAPS, capLabel, defaultCaps, num, sameCaps, statusText } from '../util.js'

// The swarm configuration form, shared by Create a swarm (Advanced configuration) and
// Settings (Defaults for new swarms). `gatewayModels` comes from GET /models and `harnesses`
// from GET /availability (both optional), to show what is really available.
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

// Harness mix (DESIGN-0.3 section 2): allowed harnesses + delivery_harnesses rows.
function HarnessMix({ cfg, setCfg, modelList, harnesses }) {
  const allowed = cfg.allowed_harnesses || ['daash']
  const rows = cfg.delivery_harnesses || []
  const info = Object.fromEntries((harnesses || []).map((h) => [h.harness, h]))
  const toggle = (h, on) => {
    const next = on ? [...allowed, h] : allowed.filter((x) => x !== h)
    setCfg({ ...cfg, allowed_harnesses: next, delivery_harnesses: rows.filter((r) => next.includes(r.harness)) })
  }
  const setRow = (i, patch) => setCfg({ ...cfg, delivery_harnesses: rows.map((r, j) => (j === i ? { ...r, ...patch } : r)) })
  const choices = HARNESS_CHOICES.filter((h) => allowed.includes(h))
  const note = (h) => {
    const s = info[h] && info[h].status
    return s && s !== 'available' ? <span className="bad"> ({statusText(s).toLowerCase()})</span> : null
  }
  return (
    <>
      <fieldset className="harness-mix">
        <legend>Allowed agents</legend>
        <div className="row">
          {HARNESS_CHOICES.map((h) => (
            <label key={h} className="inline">
              <input type="checkbox" checked={allowed.includes(h)} onChange={(e) => toggle(h, e.target.checked)} /> {h}{note(h)}
            </label>
          ))}
        </div>
        <p className="muted">Anything other than daash runs on a runner (see <a href="#/agents">Agents &amp; models</a>).
          Without a matching runner, the work falls back to the built-in agent after the fallback time below.</p>
      </fieldset>
      <fieldset className="harness-mix">
        <legend>Delivery agents</legend>
        {!rows.length && <div className="muted">No rows: every delivery agent uses the built-in daash agent.</div>}
        {rows.map((r, i) => (
          <div className="row" key={i}>
            <label>Agent for row {i + 1}
              <select value={r.harness} onChange={(e) => setRow(i, { harness: e.target.value, capabilities: null })}>
                {(choices.includes(r.harness) ? choices : [r.harness, ...choices]).map((h) => <option key={h} value={h}>{h}</option>)}
              </select></label>
            <label>Model
              <select value={r.model || ''} onChange={(e) => setRow(i, { model: e.target.value })}>
                <option value="">escalation order (default)</option>
                {(r.model && !modelList.includes(r.model) ? [r.model, ...modelList] : modelList)
                  .map((m) => <option key={m} value={m}>{m}</option>)}
              </select></label>
            <fieldset className="caps-pick">
              <legend>Capabilities (default: {defaultCaps(r.harness).map(capLabel).join(', ')})</legend>
              {OPTIONAL_CAPS.map((c) => {
                const cur = r.capabilities || defaultCaps(r.harness)
                return (
                  <label key={c} className="check"><input type="checkbox" checked={cur.includes(c)}
                    onChange={(e) => {
                      const next = e.target.checked ? [...cur, c] : cur.filter((x) => x !== c)
                      setRow(i, { capabilities: sameCaps(next, defaultCaps(r.harness)) ? null : next })
                    }} /> {capLabel(c)}</label>
                )
              })}
            </fieldset>
            <button type="button" className="btn small" aria-label={`Remove row ${i + 1}`}
              onClick={() => setCfg({ ...cfg, delivery_harnesses: rows.filter((_, j) => j !== i) })}>Remove</button>
          </div>
        ))}
        <button type="button" className="btn small" disabled={!choices.length}
          onClick={() => setCfg({ ...cfg, delivery_harnesses: [...rows, { harness: choices[0] || 'daash', provider: 'default', model: '' }] })}>
          + Add row</button>
        <p className="muted">Delivery agents cycle through these rows: agent 1 uses row 1, agent 2 row 2, and so on,
          wrapping around.</p>
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

export default function SwarmConfigFields({ cfg, setCfg, gatewayModels = [], harnesses }) {
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
      <HarnessMix cfg={cfg} setCfg={setCfg} modelList={allowedModels} harnesses={harnesses} />
    </div>
  )
}
