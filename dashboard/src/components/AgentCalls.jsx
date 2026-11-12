import React from 'react'
import { api } from '../api.js'
import { usePolling } from '../hooks/usePolling.js'
import { CONNECTED, connectedStatus, fmtNum, fmtDateTime } from '../util.js'
import { Chip, Collapsible, Empty, ErrorNote, Spinner } from './Common.jsx'
import { ActivityFeed, toolSummary } from './Feeds.jsx'

function Transcript({ transcript }) {
  if (!transcript || !transcript.length) return <Empty>No transcript recorded.</Empty>
  return (
    <div className="transcript">
      {transcript.map((m, i) => (
        <div key={i} className={`msg msg-${m.role || 'x'}`}>
          <div className="msg-role">{m.role || 'message'}{m.name ? ` (${m.name})` : ''}</div>
          <pre>{typeof m.content === 'string' ? m.content : JSON.stringify(m.content ?? m, null, 2)}
            {m.tool_calls ? '\n' + JSON.stringify(m.tool_calls, null, 2) : ''}</pre>
        </div>
      ))}
    </div>
  )
}

// DAASH tool calls and team messages of this agent (events), for agents DAASH does not run itself.
function Activity({ activity, names, teamNames, onOpenTask }) {
  const tools = activity.filter((e) => e.kind === 'tool')
  return (
    <section className="agent-activity">
      <h3>DAASH tool calls and team messages</h3>
      {!activity.length ? <Empty>None in the latest activity. An external agent calls DAASH&rsquo;s tools (memory, files, team
        messages) while it works on a task.</Empty> : (
        <>
          {tools.length > 0 && <p className="muted">{tools.length} tool call{tools.length > 1 ? 's' : ''}: {toolSummary(tools)}.</p>}
          <ActivityFeed events={activity} names={names} teamNames={teamNames} onOpenTask={onOpenTask} />
        </>
      )}
    </section>
  )
}

export default function AgentCalls({ agent, connected, activity = [], names = {}, teamNames = {}, onOpenTask }) {
  const { data, error, loading } = usePolling(() => api.getAgentCalls(agent.id, 20), [agent.id])
  const calls = (data && data.calls) || []
  const isConnected = agent.harness === CONNECTED
  return (
    <div>
      {isConnected ? (
        <div className="info-note">
          {connected ? <><strong>{connected.name}</strong> fills this delivery row: a connected agent ({connected.kind_label},{' '}
            {connected.direct ? `reached directly over ${connected.protocol_label}` : `through the agent host ${connected.runner ? connected.runner.name : ''}`}),{' '}
            now {connectedStatus(connected).text.toLowerCase()}.</> : <>A connected agent that is no longer in Agents &amp; models fills this row.</>}{' '}
          It uses its own model, so DAASH sees no model calls or tokens for it; it sees its DAASH tool calls and team messages,
          below. <a href="#/agents/connected">Agents &amp; models</a>
        </div>
      ) : (
        <p className="muted">{agent.role} · {agent.model} · {fmtNum(agent.tokens_used)} tokens · {agent.calls} calls · {agent.errors} errors</p>
      )}
      {(isConnected || (agent.harness && agent.harness !== 'daash') || activity.length > 0) && (
        <Activity activity={activity} names={names} teamNames={teamNames} onOpenTask={onOpenTask} />
      )}
      {isConnected && !calls.length ? null : <h3 className="calls-h">Model calls</h3>}
      {loading ? <Spinner label="Loading calls" /> : null}
      <ErrorNote error={error} />
      {!loading && !calls.length && !error && !isConnected && <Empty>No calls yet.</Empty>}
      {calls.map((c) => (
        <Collapsible key={c.id}
          title={<><Chip status={c.ok ? 'done' : 'failed'}>{c.ok ? 'ok' : 'error'}</Chip> {c.purpose} <span className="muted">({c.model})</span></>}
          right={<span className="muted">{fmtNum(c.total_tokens)} tok · {(c.duration_ms / 1000).toFixed(1)}s · {fmtDateTime(c.created_at)}</span>}>
          {c.error && <div className="error-note">{c.error}</div>}
          <Transcript transcript={c.transcript} />
        </Collapsible>
      ))}
    </div>
  )
}
