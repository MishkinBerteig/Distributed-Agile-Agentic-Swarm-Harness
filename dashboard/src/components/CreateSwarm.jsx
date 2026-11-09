import React, { useEffect, useState } from 'react'
import { api } from '../api.js'
import { useLoad } from '../hooks/usePolling.js'
import { HARNESS_CHOICES, OPTIONAL_CAPS, capLabel, defaultCaps, num, sameCaps } from '../util.js'
import { Collapsible, ErrorNote, Spinner, useAction } from './Common.jsx'

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

// Harness mix (DESIGN-0.3 section 2): allowed harnesses + delivery_harnesses rows.
function HarnessMix({ cfg, setCfg, modelList }) {
  const allowed = cfg.allowed_harnesses || ['daash']
  const rows = cfg.delivery_harnesses || []
  const toggle = (h, on) => {
    const next = on ? [...allowed, h] : allowed.filter((x) => x !== h)
    setCfg({ ...cfg, allowed_harnesses: next, delivery_harnesses: rows.filter((r) => next.includes(r.harness)) })
  }
  const setRow = (i, patch) => setCfg({ ...cfg, delivery_harnesses: rows.map((r, j) => (j === i ? { ...r, ...patch } : r)) })
  const choices = HARNESS_CHOICES.filter((h) => allowed.includes(h))
  return (
    <>
      <fieldset className="harness-mix">
        <legend>Allowed harnesses</legend>
        <div className="row">
          {HARNESS_CHOICES.map((h) => (
            <label key={h} className="inline">
              <input type="checkbox" checked={allowed.includes(h)} onChange={(e) => toggle(h, e.target.checked)} /> {h}
            </label>
          ))}
        </div>
        <p className="muted">Anything other than daash runs on a runner (see Runners). Without a matching runner,
          the work falls back to the built-in harness after the fallback time below.</p>
      </fieldset>
      <fieldset className="harness-mix">
        <legend>Delivery agents</legend>
        {!rows.length && <div className="muted">No rows: every delivery agent uses the built-in daash harness.</div>}
        {rows.map((r, i) => (
          <div className="row" key={i}>
            <label>Harness for row {i + 1}
              <select value={r.harness} onChange={(e) => setRow(i, { harness: e.target.value, capabilities: null })}>
                {(choices.includes(r.harness) ? choices : [r.harness, ...choices]).map((h) => <option key={h} value={h}>{h}</option>)}
              </select></label>
            <label>Model
              <select value={r.model || ''} onChange={(e) => setRow(i, { model: e.target.value })}>
                <option value="">routing (default)</option>
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

export default function CreateSwarm({ onCreated }) {
  const cfg = useLoad(api.getConfig, [])
  const models = useLoad(api.getModels, [])
  const [name, setName] = useState('')
  const [vision, setVision] = useState('')
  const [dod, setDod] = useState('')
  const [teams, setTeams] = useState([])
  const [swarmCfg, setSwarmCfg] = useState(null)
  const { busy, error, run } = useAction()

  useEffect(() => {
    if (cfg.data && !swarmCfg) setSwarmCfg(structuredClone(cfg.data.default_swarm_config))
  }, [cfg.data, swarmCfg])

  const modelList = (models.data && models.data.models) || (swarmCfg && swarmCfg.allowed_models) || []
  const setRole = (k, v) => setSwarmCfg({ ...swarmCfg, role_models: { ...swarmCfg.role_models, [k]: v } })
  const setLimit = (k, v) => setSwarmCfg({ ...swarmCfg, limits: { ...swarmCfg.limits, [k]: num(v) ?? null } })
  const setTeam = (i, patch) => setTeams(teams.map((t, j) => (j === i ? { ...t, ...patch } : t)))

  const submit = (e) => {
    e.preventDefault()
    const payload = {
      name: name.trim(),
      vision_statement: vision.trim(),
      definition_of_done: dod.trim() || null,
      teams: teams.filter((t) => t.name.trim() && t.mission_statement.trim())
        .map((t) => ({ name: t.name.trim(), mission_statement: t.mission_statement.trim() })),
    }
    if (swarmCfg) payload.config = swarmCfg
    run(async () => { await api.createSwarm(payload); onCreated() })
  }

  return (
    <form className="card form" onSubmit={submit}>
      <h2>Create a swarm</h2>
      {models.data && models.data.reachable === false && (
        <div className="warn-note">LLM gateway at {models.data.endpoint} is not reachable; model lists may be incomplete.</div>
      )}
      <label>Name<input required value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Launch blog" /></label>
      <label>Vision<textarea required rows={4} value={vision} onChange={(e) => setVision(e.target.value)}
        placeholder="What outcome should the swarm achieve?" /></label>
      <label>Vision Definition of Done <span className="muted">(optional; the Swarm Coordinator drafts one if blank)</span>
        <textarea rows={2} value={dod} onChange={(e) => setDod(e.target.value)} /></label>

      <fieldset>
        <legend>Teams <span className="muted">(leave empty and the Swarm Coordinator forms teams)</span></legend>
        {teams.map((t, i) => (
          <div className="row" key={i}>
            <input placeholder="Team name" value={t.name} onChange={(e) => setTeam(i, { name: e.target.value })} />
            <input className="grow" placeholder="Mission" value={t.mission_statement}
              onChange={(e) => setTeam(i, { mission_statement: e.target.value })} />
            <button type="button" className="btn small" onClick={() => setTeams(teams.filter((_, j) => j !== i))}>Remove</button>
          </div>
        ))}
        <button type="button" className="btn small" onClick={() => setTeams([...teams, { name: '', mission_statement: '' }])}>+ Add team</button>
      </fieldset>

      <Collapsible title="Advanced configuration">
        {!swarmCfg ? (cfg.error ? <ErrorNote error={cfg.error} /> : <Spinner label="Loading defaults" />) : (
          <div className="adv">
            <div className="grid-2">
              {ROLES.map(([k, label]) => (
                <label key={k}>{label} model
                  <ModelSelect models={modelList} value={swarmCfg.role_models[k]} onChange={(v) => setRole(k, v)} /></label>
              ))}
              <label>Coordinator model
                <ModelSelect models={modelList} value={swarmCfg.coordinator.model}
                  onChange={(v) => setSwarmCfg({ ...swarmCfg, coordinator: { ...swarmCfg.coordinator, model: v } })} /></label>
              <label>Delivery agents per team
                <input type="number" min="1" max="5" value={swarmCfg.delivery_agents_per_team}
                  onChange={(e) => setSwarmCfg({ ...swarmCfg, delivery_agents_per_team: num(e.target.value) ?? 1 })} /></label>
              {LIMITS.map(([k, label, min]) => (
                <label key={k}>{label}
                  <input type="number" min={min} value={swarmCfg.limits[k] ?? ''} placeholder="unlimited"
                    onChange={(e) => setLimit(k, e.target.value)} /></label>
              ))}
            </div>
            <HarnessMix cfg={swarmCfg} setCfg={setSwarmCfg} modelList={modelList} />
          </div>
        )}
      </Collapsible>

      <ErrorNote error={error} />
      <div><button className="btn primary" disabled={busy || !name.trim() || !vision.trim()}>
        {busy ? <Spinner label="Creating" /> : 'Create swarm'}</button></div>
    </form>
  )
}
