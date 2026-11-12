import React, { useState } from 'react'
import { api } from '../api.js'
import { usePolling } from '../hooks/usePolling.js'
import { eventText, fmtDateTime, fmtTime, toolOfEvent } from '../util.js'
import { Chip, Empty, ErrorNote, Spinner } from './Common.jsx'

// Consecutive DAASH tool calls of one agent (events of kind `tool`, newest first) become one group,
// so a busy external agent does not drown the feed.
export function groupToolCalls(events) {
  const out = []
  for (const e of events) {
    const last = out[out.length - 1]
    if (e.kind === 'tool' && last && last.tools && last.actor === e.actor) last.events.push(e)
    else if (e.kind === 'tool') out.push({ tools: true, key: `g-${e.id}`, actor: e.actor, team_id: e.team_id, events: [e] })
    else out.push({ key: e.id, event: e })
  }
  // keyed by the group's oldest call, so an open group stays open while new calls arrive on top
  return out.map((g) => (!g.tools ? g : g.events.length === 1 ? { key: g.events[0].id, event: g.events[0] }
    : { ...g, key: `g-${g.events[g.events.length - 1].id}` }))
}

// "write_file ×3, read_file ×2, remember"
export function toolSummary(events) {
  const counts = new Map()
  for (const e of events) { const t = toolOfEvent(e); counts.set(t, (counts.get(t) || 0) + 1) }
  return [...counts.entries()].map(([t, n]) => (n > 1 ? `${t} ×${n}` : t)).join(', ')
}

function EventLine({ e, names, teamNames, onOpenTask }) {
  return (
    <li className={e.kind === 'message' ? 'feed-message' : e.kind === 'tool' ? 'feed-tool' : undefined}>
      <span className="muted feed-time" title={fmtDateTime(e.created_at)}>{fmtTime(e.created_at)}</span>
      <Chip status={e.kind} />
      {e.kind !== 'message' && <strong>{names[e.actor] || e.actor}</strong>}
      {e.team_id && teamNames[e.team_id] && <span className="muted"> [{teamNames[e.team_id]}]</span>}
      <span> {eventText(e)}</span>
      {e.task_id && onOpenTask && <>{' '}<button className="link" onClick={() => onOpenTask(e.task_id)}>open task</button></>}
    </li>
  )
}

function ToolGroup({ g, names, teamNames, onOpenTask }) {
  const [open, setOpen] = useState(false)
  const newest = g.events[0]
  return (
    <li className="feed-tool">
      <span className="muted feed-time" title={fmtDateTime(newest.created_at)}>{fmtTime(newest.created_at)}</span>
      <Chip status="tool" />
      <strong>{names[g.actor] || g.actor}</strong>
      {g.team_id && teamNames[g.team_id] && <span className="muted"> [{teamNames[g.team_id]}]</span>}
      <span> used DAASH tools {g.events.length} times: {toolSummary(g.events)}</span>{' '}
      <button type="button" className="link" aria-expanded={open} onClick={() => setOpen(!open)}>{open ? 'hide' : 'show each'}</button>
      {open && (
        <ul className="feed-sub">
          {g.events.map((e) => <EventLine key={e.id} e={e} names={names} teamNames={teamNames} onOpenTask={onOpenTask} />)}
        </ul>
      )}
    </li>
  )
}

export function ActivityFeed({ events, names, teamNames, onOpenTask }) {
  const [toolMode, setToolMode] = useState('grouped')   // grouped | each | hidden
  const [onlyMessages, setOnlyMessages] = useState(false)
  if (!events) return <Spinner label="Loading events" />
  if (!events.length) return <Empty>No activity yet.</Empty>
  const tools = events.filter((e) => e.kind === 'tool').length
  const messages = events.filter((e) => e.kind === 'message').length
  let shown = onlyMessages ? events.filter((e) => e.kind === 'message') : events
  if (toolMode === 'hidden') shown = shown.filter((e) => e.kind !== 'tool')
  const items = toolMode === 'grouped' ? groupToolCalls(shown) : shown.map((e) => ({ key: e.id, event: e }))
  return (
    <>
      {(tools > 0 || messages > 0) && (
        <div className="feed-filters">
          <label className="inline"><input type="checkbox" checked={onlyMessages} onChange={(e) => setOnlyMessages(e.target.checked)} />
            Team messages only ({messages})</label>
          {tools > 0 && (
            <label className="inline">DAASH tool calls ({tools})
              <select value={toolMode} onChange={(e) => setToolMode(e.target.value)} aria-label="How to show DAASH tool calls">
                <option value="grouped">grouped by agent</option>
                <option value="each">each call</option>
                <option value="hidden">hidden</option>
              </select></label>
          )}
        </div>
      )}
      {!items.length ? <Empty>{onlyMessages ? 'No team messages yet. Agents send them with the send_message tool.' : 'Nothing to show.'}</Empty> : (
        <ul className="feed">
          {items.map((it) => (it.event
            ? <EventLine key={it.key} e={it.event} names={names} teamNames={teamNames} onOpenTask={onOpenTask} />
            : <ToolGroup key={it.key} g={it} names={names} teamNames={teamNames} onOpenTask={onOpenTask} />))}
        </ul>
      )}
    </>
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
