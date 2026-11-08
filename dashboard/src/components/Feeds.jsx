import React from 'react'
import { api } from '../api.js'
import { usePolling } from '../hooks/usePolling.js'
import { eventText, fmtDateTime, fmtTime } from '../util.js'
import { Chip, Empty, ErrorNote, Spinner } from './Common.jsx'

export function ActivityFeed({ events, names, teamNames, onOpenTask }) {
  if (!events) return <Spinner label="Loading events" />
  if (!events.length) return <Empty>No activity yet.</Empty>
  return (
    <ul className="feed">
      {events.map((e) => (
        <li key={e.id}>
          <span className="muted feed-time" title={fmtDateTime(e.created_at)}>{fmtTime(e.created_at)}</span>
          <Chip status={e.kind} />
          <strong>{names[e.actor] || e.actor}</strong>
          {e.team_id && teamNames[e.team_id] && <span className="muted"> [{teamNames[e.team_id]}]</span>}
          <span> {eventText(e)}</span>
          {e.task_id && <button className="link" onClick={() => onOpenTask(e.task_id)}>task</button>}
        </li>
      ))}
    </ul>
  )
}

export function DecisionBus({ teamNames }) {
  const { data, error, loading } = usePolling(() => api.getDecisions(50), [])
  const signals = (data && data.signals) || []
  if (loading) return <Spinner label="Loading decisions" />
  return (
    <>
      <ErrorNote error={error} />
      {!signals.length ? <Empty>No bus signals.</Empty> : (
        <ul className="feed">
          {signals.map((s, i) => (
            <li key={s.id || i}>
              <span className="muted feed-time">{fmtTime(s.created_at || s.timestamp)}</span>
              <Chip status={s.kind} />
              {s.team_id && <span className="muted">[{teamNames[s.team_id] || s.team_id}]</span>}
              <code className="payload"> {JSON.stringify(s.payload || {})}</code>
            </li>
          ))}
        </ul>
      )}
    </>
  )
}
