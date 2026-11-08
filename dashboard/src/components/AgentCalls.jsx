import React from 'react'
import { api } from '../api.js'
import { usePolling } from '../hooks/usePolling.js'
import { fmtNum, fmtDateTime } from '../util.js'
import { Chip, Collapsible, Empty, ErrorNote, Spinner } from './Common.jsx'

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

export default function AgentCalls({ agent }) {
  const { data, error, loading } = usePolling(() => api.getAgentCalls(agent.id, 20), [agent.id])
  const calls = (data && data.calls) || []
  return (
    <div>
      <p className="muted">{agent.role} · {agent.model} · {fmtNum(agent.tokens_used)} tokens · {agent.calls} calls · {agent.errors} errors</p>
      {loading ? <Spinner label="Loading calls" /> : null}
      <ErrorNote error={error} />
      {!loading && !calls.length && !error && <Empty>No calls yet.</Empty>}
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
