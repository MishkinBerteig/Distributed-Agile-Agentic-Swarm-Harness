import React from 'react'
import { api } from '../api.js'
import { usePolling } from '../hooks/usePolling.js'
import { fmtDateTime } from '../util.js'
import { Chip, Empty, ErrorNote, Spinner } from './Common.jsx'

export function Learnings({ swarmId, teamNames }) {
  const { data, error, loading } = usePolling(() => api.getLearnings(swarmId), [swarmId])
  const items = (data && data.learnings) || []
  if (loading) return <Spinner label="Loading learnings" />
  return (
    <>
      <ErrorNote error={error} />
      {!items.length ? <Empty>No learnings recorded yet. They appear as work completes and in the LEARNING phase.</Empty> : (
        <ul className="feed">
          {items.map((l) => (
            <li key={l.id}>
              <Chip status={l.kind} />
              {l.swarm_id ? null : <span className="chip s-global">global</span>}
              {l.team_id && <span className="muted"> [{teamNames[l.team_id] || l.team_id}]</span>}
              <span className="prewrap"> {l.content}</span>
              <span className="muted"> · {fmtDateTime(l.created_at)}</span>
            </li>
          ))}
        </ul>
      )}
    </>
  )
}

export function Proposals({ swarmId, teamNames, names }) {
  const { data, error, loading } = usePolling(() => api.getProposals(swarmId), [swarmId])
  const items = (data && data.proposals) || []
  if (loading) return <Spinner label="Loading proposals" />
  return (
    <>
      <ErrorNote error={error} />
      {!items.length ? <Empty>No team spin-out proposals.</Empty> : (
        <ul className="feed">
          {items.map((p) => {
            const votes = Object.entries(p.votes || {})
            const yes = votes.filter(([, v]) => v).length
            return (
              <li key={p.id}>
                <Chip status={p.status} />
                <strong> {p.name}</strong> proposed by {names[p.proposer_agent_id] || p.proposer_agent_id}
                {teamNames[p.team_id] && <span className="muted"> ({teamNames[p.team_id]})</span>}
                <div className="prewrap">{p.mission_statement}</div>
                {p.reason && <div className="muted">Reason: {p.reason}</div>}
                <div className="muted">
                  Votes: {yes} yes / {votes.length - yes} no (quorum {p.quorum})
                  {votes.length > 0 && <> — {votes.map(([a, v]) => `${names[a] || a}: ${v ? 'yes' : 'no'}`).join(', ')}</>}
                </div>
              </li>
            )
          })}
        </ul>
      )}
    </>
  )
}
