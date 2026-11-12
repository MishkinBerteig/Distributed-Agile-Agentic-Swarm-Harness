import React, { useEffect, useState } from 'react'
import { api } from '../api.js'
import { useLoad, usePolling } from '../hooks/usePolling.js'
import { Collapsible, ErrorNote, Spinner, useAction } from './Common.jsx'
import SwarmConfigFields, { connectedProblem } from './SwarmConfigFields.jsx'
import { CONNECTED } from '../util.js'

export default function CreateSwarm({ onCreated }) {
  const cfg = useLoad(api.getConfig, [])
  const models = useLoad(api.getModels, [])
  // polled: this form stays mounted while other pages are open, and agents get connected there
  const avail = usePolling(() => api.getAvailability(), [], { interval: 15000 })
  const [name, setName] = useState('')
  const [vision, setVision] = useState('')
  const [dod, setDod] = useState('')
  const [teams, setTeams] = useState([])
  const [swarmCfg, setSwarmCfg] = useState(null)
  const { busy, error, run } = useAction()

  useEffect(() => {
    if (cfg.data && !swarmCfg) setSwarmCfg(structuredClone(cfg.data.default_swarm_config))
  }, [cfg.data, swarmCfg])

  const gatewayModels = (models.data && models.data.models) || []
  // Problems with connected agents in the delivery rows, shown by the button too (Advanced may be closed).
  const connectedList = avail.data && avail.data.connected_agents
  const byId = Object.fromEntries((connectedList || []).map((a) => [a.id, a]))
  const rowProblems = ((swarmCfg && swarmCfg.delivery_harnesses) || [])
    .map((r, i) => [i, r.harness === CONNECTED ? connectedProblem(r, byId, connectedList !== undefined) : null])
    .filter(([, p]) => p)
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

      <p className="muted">Uses the defaults from <a href="#/settings">Settings</a> unless you change them under
        Advanced configuration. See what is available in <a href="#/agents">Agents &amp; models</a>.</p>
      <Collapsible title="Advanced configuration">
        {!swarmCfg ? (cfg.error ? <ErrorNote error={cfg.error} /> : <Spinner label="Loading defaults" />) : (
          <SwarmConfigFields cfg={swarmCfg} setCfg={setSwarmCfg} gatewayModels={gatewayModels}
            harnesses={avail.data && avail.data.harnesses} connected={avail.data && avail.data.connected_agents} />
        )}
      </Collapsible>

      {rowProblems.length > 0 && (
        <div className="warn-note">
          <strong>Delivery agents</strong> (under Advanced configuration):
          <ul className="plain-list">{rowProblems.map(([i, p]) => <li key={i}>Row {i + 1}: {p}</li>)}</ul>
        </div>
      )}
      <ErrorNote error={error} />
      <div><button className="btn primary" disabled={busy || !name.trim() || !vision.trim()}>
        {busy ? <Spinner label="Creating" /> : 'Create swarm'}</button></div>
    </form>
  )
}
