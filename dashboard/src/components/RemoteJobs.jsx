import React from 'react'
import { api } from '../api.js'
import { usePolling } from '../hooks/usePolling.js'
import { ago, connectedBadge } from '../util.js'
import { Chip } from './Common.jsx'

// Compact list of remote jobs; hidden when the swarm has none.
export default function RemoteJobs({ swarmId, tasksById, runnersById, connectedById = {} }) {
  // A connected agent's job names the agent (and, on an agent host, its runner) in its payload.
  const agentOf = (j) => j.harness === 'connected' && j.payload && connectedById[j.payload.connected_agent_id]
  const jobs = usePolling(() => api.listRemoteJobs(swarmId), [swarmId])
  const list = (jobs.data && jobs.data.jobs) || []
  if (!list.length) return null
  const shown = list.slice().sort((a, b) => String(b.created_at).localeCompare(String(a.created_at))).slice(0, 15)
  return (
    <section className="card remote-jobs" aria-label="Remote jobs">
      <h3>Remote jobs <span className="muted">({list.length})</span></h3>
      <div className="table-scroll">
        <table className="table compact">
          <thead><tr><th>Status</th><th>Harness</th><th>Model</th><th>Runner</th><th>Task</th><th>Age</th></tr></thead>
          <tbody>
            {shown.map((j) => (
              <tr key={j.id}>
                <td><Chip status={j.status} /></td>
                <td>{agentOf(j) ? <span className="chip connected-badge">{connectedBadge(agentOf(j))}</span> : j.harness}</td>
                <td>{j.model || (j.harness === 'connected' ? <span className="muted">its own</span> : '-')}</td>
                <td>{j.runner_name || (runnersById && runnersById[j.runner_id]) || j.runner_id
                  || (agentOf(j) && agentOf(j).runner ? agentOf(j).runner.name : agentOf(j) ? <span className="muted">direct</span> : '-')}</td>
                <td className="clip">{(j.task && j.task.name) || tasksById[j.task_id]?.name || j.task_id}</td>
                <td>{ago(j.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}
