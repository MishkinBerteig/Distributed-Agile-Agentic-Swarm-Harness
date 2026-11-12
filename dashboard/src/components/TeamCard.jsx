import React, { useState } from 'react'
import { api } from '../api.js'
import { capLabel, creatorLabel, fmtNum } from '../util.js'
import { Chip, Collapsible, ErrorNote, Spinner, useAction } from './Common.jsx'
import Kanban from './Kanban.jsx'
import AddTask from './AddTask.jsx'

function Roster({ agents, tasksById, onAgent }) {
  if (!agents.length) return <div className="muted">No agents.</div>
  return (
    <div className="table-scroll">
    <table className="table compact roster">
      <thead><tr><th>Role</th><th>Name</th><th>Model</th><th>State</th><th>Task</th><th>Tokens</th><th>Calls/Err</th></tr></thead>
      <tbody>
        {agents.map((a) => (
          <tr key={a.id}>
            <td>{a.role.replace(/_/g, ' ')}</td>
            <td><button type="button" className="link" onClick={() => onAgent(a)}>{a.name}</button></td>
            <td>{a.model}{a.harness && a.harness !== 'daash' && (
              <> <span className="chip harness-badge" title={`harness: ${a.harness}`}>{a.harness}</span></>
            )}
              {(a.capabilities || []).length > 0 && (
                <div className="caps" title={`capabilities: ${a.capabilities.map(capLabel).join(', ')}`}>
                  {a.capabilities.map((c) => <span key={c} className="chip cap-chip">{capLabel(c)}</span>)}
                </div>
              )}</td>
            <td><Chip status={a.state} /></td>
            <td className="clip">{a.current_task_id ? (tasksById[a.current_task_id]?.name || a.current_task_id) : '-'}</td>
            <td>{fmtNum(a.tokens_used)}</td>
            <td>{a.calls}/<span className={a.errors ? 'bad' : ''}>{a.errors}</span></td>
          </tr>
        ))}
      </tbody>
    </table>
    </div>
  )
}

function Standards({ team, onChanged }) {
  const [dor, setDor] = useState('')
  const [dod, setDod] = useState('')
  const { busy, error, run } = useAction()
  const regen = () => run(async () => { await api.generateQuality(team.id, dor, dod); onChanged() })
  return (
    <Collapsible title="Definition of Ready / Done">
      <h4>Definition of Ready (team additions)</h4>
      <p className="prewrap">{team.definition_of_ready || <span className="muted">Not generated yet.</span>}</p>
      <h4>Definition of Done (team mission level)</h4>
      <p className="prewrap">{team.definition_of_done || <span className="muted">Not generated yet.</span>}</p>
      <div className="form regen">
        <label>DoR guidance<input value={dor} onChange={(e) => setDor(e.target.value)} /></label>
        <label>DoD guidance<input value={dod} onChange={(e) => setDod(e.target.value)} /></label>
        <ErrorNote error={error} />
        <div><button className="btn small" onClick={regen} disabled={busy}>
          {busy ? <Spinner label="Quality Judgement is working (can take a while)" /> : 'Regenerate with guidance'}</button></div>
      </div>
    </Collapsible>
  )
}

export default function TeamCard({ entry, teamsById, onOpenTask, onAgent, onChanged }) {
  const { team, agents, tasks } = entry
  const agentsById = Object.fromEntries(agents.map((a) => [a.id, a]))
  const tasksById = Object.fromEntries(tasks.map((t) => [t.id, t]))
  return (
    <article className="card team">
      <div className="team-head">
        <h3>{team.name}</h3>
        <Chip status={team.status} />
      </div>
      <p className="mission">{team.mission_statement}</p>
      <div className="muted team-meta">
        created by {creatorLabel(team, teamsById)} · generation {team.generation}
        {team.next_focus && <> · focus: {team.next_focus}</>}
      </div>
      <Roster agents={agents} tasksById={tasksById} onAgent={onAgent} />
      <Standards team={team} onChanged={onChanged} />
      <Kanban tasks={tasks} agentsById={agentsById} onOpen={onOpenTask} />
      <AddTask team={team} onAdded={onChanged} />
    </article>
  )
}
