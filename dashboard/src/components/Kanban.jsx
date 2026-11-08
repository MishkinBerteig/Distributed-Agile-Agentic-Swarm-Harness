import React, { useState } from 'react'
import { TASK_COLUMNS, TERMINAL_COLUMNS, criteriaSummary, statusClass } from '../util.js'

function Card({ task, agentName, onOpen }) {
  const c = criteriaSummary(task)
  return (
    <button type="button" className={`kcard ${statusClass(task.status)}`} onClick={() => onOpen(task.id)}>
      <div className="kcard-name">{task.name}</div>
      <div className="kcard-meta">
        {task.priority != null && <span title="priority (lower = sooner)">P{task.priority}</span>}
        {task.complexity && <span>{task.complexity}</span>}
        <span title="attempts">try {task.attempts}</span>
        {c.total > 0 && (
          <span className={`crit ${c.red ? 'crit-red' : c.green === c.total ? 'crit-green' : ''}`}
            title="criteria green / total">{c.green}/{c.total}{c.red ? ` (${c.red} red)` : ''}</span>
        )}
      </div>
      {agentName && <div className="kcard-agent">{agentName}</div>}
    </button>
  )
}

export default function Kanban({ tasks, agentsById, onOpen }) {
  const [openTerm, setOpenTerm] = useState({})
  const by = (s) => tasks
    .filter((t) => t.status === s)
    .sort((a, b) => (a.priority ?? 1e9) - (b.priority ?? 1e9))
  const agentName = (t) => t.assigned_agent_id && agentsById[t.assigned_agent_id]?.name

  return (
    <div className="kanban">
      <div className="kcols">
        {TASK_COLUMNS.map((s) => (
          <div key={s} className={`kcol ${statusClass(s)}`}>
            <div className="kcol-head">{s} <span className="count">{by(s).length}</span></div>
            {by(s).map((t) => <Card key={t.id} task={t} agentName={agentName(t)} onOpen={onOpen} />)}
          </div>
        ))}
      </div>
      <div className="kterm">
        {TERMINAL_COLUMNS.map((s) => (
          <div key={s} className="kterm-item">
            <button type="button" className={`chip ${statusClass(s)} chip-btn`}
              onClick={() => setOpenTerm({ ...openTerm, [s]: !openTerm[s] })} aria-expanded={!!openTerm[s]}>
              {openTerm[s] ? '▾' : '▸'} {s} <b>{by(s).length}</b>
            </button>
            {openTerm[s] && by(s).map((t) => <Card key={t.id} task={t} agentName={agentName(t)} onOpen={onOpen} />)}
          </div>
        ))}
      </div>
    </div>
  )
}
