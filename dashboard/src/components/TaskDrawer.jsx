import React, { useState } from 'react'
import { api } from '../api.js'
import { usePolling } from '../hooks/usePolling.js'
import { fmtDateTime, fmtTime } from '../util.js'
import { Chip, Empty, ErrorNote, Spinner, useAction } from './Common.jsx'
import Modal from './Modal.jsx'

function Criterion({ c }) {
  return (
    <li className="criterion">
      <div><Chip status={c.status} /> {c.text}</div>
      <div className="muted">
        {c.check_kind}{c.check_path ? ` · ${c.check_path}` : ''}{c.check_arg ? ` · ${c.check_arg}` : ''}
        {c.score != null ? ` · score ${Number(c.score).toFixed(3)}` : ''}
      </div>
      {c.evidence && <div className="evidence">{c.evidence}</div>}
    </li>
  )
}

function Controls({ task, onChanged, onClose }) {
  const [reason, setReason] = useState('')
  const { busy, error, run } = useAction()
  const reopen = () => run(async () => { await api.updateTask(task.id, { status: 'Pending' }); onChanged() })
  const reject = () => run(async () => {
    await api.updateTask(task.id, { status: 'Rejected', rejection_reason: reason.trim() }); setReason(''); onChanged()
  })
  const del = () => {
    if (!window.confirm(`Delete task "${task.name}"?`)) return
    run(async () => { await api.deleteTask(task.id); onClose() })
  }
  return (
    <section>
      <h4>User controls</h4>
      <div className="row">
        <button className="btn small" disabled={busy || task.status === 'Pending'} onClick={reopen}>Reopen to Pending</button>
        <button className="btn small danger" disabled={busy} onClick={del}>Delete</button>
      </div>
      <div className="row">
        <input className="grow" placeholder="Rejection reason" value={reason} onChange={(e) => setReason(e.target.value)} />
        <button className="btn small" disabled={busy || !reason.trim() || task.status === 'Rejected'} onClick={reject}>Reject</button>
      </div>
      <ErrorNote error={error} />
    </section>
  )
}

export default function TaskDrawer({ taskId, names, onClose, onChanged, onOpenTask, onOpenFile }) {
  const { data: task, error, loading, refresh } = usePolling(() => api.getTask(taskId), [taskId])
  const changed = () => { refresh(); onChanged() }
  const actor = (id) => names[id] || id
  const files = (task && task.deliverable && task.deliverable.files) || []

  return (
    <Modal title={task ? task.name : 'Task'} onClose={onClose}>
      {loading && !task && <Spinner label="Loading task" />}
      {error && !task && <ErrorNote error={error} />}
      {task && (
        <div className="task-detail">
          <div className="row">
            <Chip status={task.status} />
            {task.priority != null && <span>priority {task.priority}</span>}
            {task.complexity && <span>{task.complexity}</span>}
            <span>attempts {task.attempts}</span>
            {task.assigned_agent_id && <span>agent {actor(task.assigned_agent_id)}</span>}
          </div>
          <ErrorNote error={error} />
          {task.rejection_reason && <div className="warn-note">Rejected: {task.rejection_reason}</div>}
          {task.feedback && <div className="warn-note">Latest judge feedback: {task.feedback}</div>}
          <h4>Description</h4>
          <p className="prewrap">{task.description || <span className="muted">none</span>}</p>
          <h4>Artifact</h4>
          <p>{task.artifact ? <button className="link" onClick={() => onOpenFile(task.artifact)}>{task.artifact}</button> : <span className="muted">none</span>}</p>

          <h4>Acceptance criteria</h4>
          {(task.criteria || []).length ? <ul className="criteria">{task.criteria.map((c) => <Criterion key={c.id} c={c} />)}</ul> : <Empty>No criteria yet.</Empty>}

          <h4>Verdicts</h4>
          {(task.verdicts || []).length ? task.verdicts.map((v) => (
            <div key={v.id} className="verdict">
              <div><Chip status={v.passed ? 'done' : 'failed'}>{v.passed ? 'pass' : 'fail'}</Chip>{' '}
                <strong>{v.judge_role.replace(/_/g, ' ')}</strong> · attempt {v.attempt} · score {Number(v.score).toFixed(3)}
                {v.agent_id && <span className="muted"> · {actor(v.agent_id)}</span>}</div>
              <p className="prewrap">{v.reasoning}</p>
            </div>
          )) : <Empty>No verdicts yet.</Empty>}

          <h4>Deliverable</h4>
          {task.deliverable && (task.deliverable.summary || files.length) ? (
            <>
              <p className="prewrap">{task.deliverable.summary}</p>
              <ul className="files">{files.map((f) => {
                const path = typeof f === 'string' ? f : f.path
                return <li key={path}><button className="link" onClick={() => onOpenFile(path)}>{path}</button></li>
              })}</ul>
            </>
          ) : <Empty>Nothing submitted yet.</Empty>}

          {(task.children || []).length > 0 && (
            <>
              <h4>Subtasks</h4>
              <ul>{task.children.map((c) => (
                <li key={c.id}><button className="link" onClick={() => onOpenTask(c.id)}>{c.name}</button> <Chip status={c.status} /></li>
              ))}</ul>
            </>
          )}

          <h4>Timeline</h4>
          {(task.events || []).length ? (
            <ol className="timeline">
              {[...task.events].sort((a, b) => String(a.created_at).localeCompare(String(b.created_at))).map((e) => (
                <li key={e.id}>
                  <span className="muted" title={fmtDateTime(e.created_at)}>{fmtTime(e.created_at)}</span>{' '}
                  <Chip status={e.kind} /> <strong>{actor(e.actor)}</strong> {e.detail}
                  {(e.from_status || e.to_status) && <span className="muted"> ({e.from_status || '?'} to {e.to_status || '?'})</span>}
                </li>
              ))}
            </ol>
          ) : <Empty>No events.</Empty>}

          <Controls task={task} onChanged={changed} onClose={() => { onChanged(); onClose() }} />
        </div>
      )}
    </Modal>
  )
}
