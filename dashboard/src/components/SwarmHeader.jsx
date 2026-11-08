import React, { useState } from 'react'
import { api } from '../api.js'
import { ACTION_LABEL, SWARM_ACTIONS, actionsFor, fmtNum } from '../util.js'
import { Bar, Chip, Collapsible, ErrorNote, useAction } from './Common.jsx'

function Budgets({ metrics }) {
  const l = metrics && metrics.limits
  if (!l) return null
  return (
    <div className="budgets">
      {l.max_total_tokens ? (
        <Bar label="Tokens" value={l.tokens_used || 0} max={l.max_total_tokens}
          text={`${fmtNum(l.tokens_used || 0)} / ${fmtNum(l.max_total_tokens)}`} />
      ) : <div className="muted">Tokens: {fmtNum(l.tokens_used || 0)} (no limit)</div>}
      {l.max_wall_clock_minutes ? (
        <Bar label="Wall clock" value={l.elapsed_minutes || 0} max={l.max_wall_clock_minutes}
          text={`${Math.round(l.elapsed_minutes || 0)} / ${l.max_wall_clock_minutes} min`} />
      ) : <div className="muted">Elapsed: {Math.round(l.elapsed_minutes || 0)} min (no limit)</div>}
      {l.max_budget_usd ? (
        <Bar label="Cost" value={l.cost_usd || 0} max={l.max_budget_usd}
          text={`$${(l.cost_usd || 0).toFixed(2)} / $${l.max_budget_usd}`} />
      ) : l.cost_usd ? <div className="muted">Cost: ${l.cost_usd.toFixed(2)}</div> : null}
    </div>
  )
}

export default function SwarmHeader({ swarm, metrics, onChanged }) {
  const { busy, error, run } = useAction()
  const [fb, setFb] = useState('')
  const state = swarm.lifecycle_state

  const act = (a) => {
    if (a === 'delete' && !window.confirm(`Delete swarm "${swarm.name}"?`)) return
    run(async () => {
      if (a === 'delete') await api.deleteSwarm(swarm.id)
      else await api.transition(swarm.id, a)
      onChanged()
    })
  }
  const sendFeedback = () => run(async () => {
    await api.postFeedback(swarm.id, fb.trim()); setFb(''); onChanged()
  })

  return (
    <header className="card swarm-head">
      <div className="head-top">
        <div>
          <h1>{swarm.name}</h1>
          <Chip status={state} />
        </div>
        <div className="actions">
          {actionsFor(state).map((a) => (
            <button key={a} className={`btn ${a === 'delete' ? 'danger' : a === 'start' || a === 'resume' ? 'primary' : ''}`}
              disabled={busy} onClick={() => act(a)}
              title={`${state} -> ${SWARM_ACTIONS[a][state]}`}>{ACTION_LABEL[a]}</button>
          ))}
        </div>
      </div>
      {swarm.paused_reason && state !== 'ACTIVE' && (
        <div className="warn-note"><strong>Paused:</strong> {swarm.paused_reason}</div>
      )}
      <ErrorNote error={error} />
      {state === 'PAUSED' && (
        <div className="feedback-box">
          <textarea rows={2} value={fb} onChange={(e) => setFb(e.target.value)}
            placeholder="Give the swarm feedback (moves it to USER_FEEDBACK; then Resume)" />
          <button className="btn" disabled={busy || !fb.trim()} onClick={sendFeedback}>Send feedback</button>
        </div>
      )}
      <Budgets metrics={metrics} />
      <Collapsible title="Vision and Definition of Done" defaultOpen>
        <p className="prewrap">{swarm.vision_statement}</p>
        <h4>Vision Definition of Done</h4>
        <p className="prewrap">{swarm.definition_of_done || <span className="muted">Not yet defined (the Swarm Coordinator drafts it on bootstrap).</span>}</p>
      </Collapsible>
    </header>
  )
}
