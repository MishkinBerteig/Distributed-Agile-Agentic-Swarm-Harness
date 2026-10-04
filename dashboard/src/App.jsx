import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from './api.js'

const STATUSES = ['Pending', 'Ready', 'In-Progress', 'Done']

function shortId(id) {
  return id ? id.slice(0, 8) : ''
}

// -- Lifecycle helpers --

const LIFECYCLE_COLORS = {
  CREATED:     '#f0ad4e',
  ACTIVE:      '#5cb85c',
  PAUSED:      '#d3d3d3',
  USER_FEEDBACK: '#5bc0de',
  VERIFICATION: '#9b59b6',
  LEARNING:    '#1abc9c',
  ARCHIVED:    '#bdc3c7',
  DELETED:     '#95a5a6',
}

const ALLOWED_ACTIONS = {
  CREATED:     [{ action: 'start',     label: '▶ Start' }],
  ACTIVE:      [{ action: 'pause',     label: '⏸ Pause' },
                 { action: 'verify',    label: '✓ Verify' }],
  PAUSED:      [{ action: 'resume',    label: '▶ Resume' },
                 { action: 'feedback',  label: '💬 Feedback' },
                 { action: 'verify',    label: '✓ Verify' }],
  USER_FEEDBACK: [{ action: 'resume',  label: '▶ Resume' }],
  VERIFICATION:[{ action: 'learn',     label: '📚 Learn' },
                 { action: 'archive',   label: '📦 Archive' },
                 { action: 'delete',    label: '🗑 Delete' }],
  LEARNING:    [{ action: 'archive',   label: '📦 Archive' }],
  ARCHIVED:    [],
  DELETED:     [],
}

function LifecycleBadge({ state }) {
  const bg = LIFECYCLE_COLORS[state] || '#999'
  return (
    <span className="chip" style={{background: bg, color: '#fff'}}>
      {state}
    </span>
  )
}

// -- Swarm form --

function SwarmForm({ onCreated, onError }) {
  const [name, setName] = useState('')
  const [vision, setVision] = useState('')
  const [mission, setMission] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit(e) {
    e.preventDefault()
    if (!name.trim()) return
    setBusy(true)
    try {
      await api.createSwarm({ name: name.trim(), vision_statement: vision, mission_statement: mission })
      setName(''); setVision(''); setMission('')
      onCreated()
    } catch (err) {
      onError(explain('Creating swarm', err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="card form" onSubmit={submit}>
      <h2>Create swarm</h2>
      <label>
        Name
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Alpha Swarm" required />
      </label>
      <label>
        Vision statement
        <input value={vision} onChange={(e) => setVision(e.target.value)} placeholder="Ship verified value daily" />
      </label>
      <label>
        Mission statement
        <input value={mission} onChange={(e) => setMission(e.target.value)} placeholder="Automate the boring parts" />
      </label>
      <button type="submit" disabled={busy || !name.trim()}>{busy ? 'Creating…' : 'Create swarm'}</button>
    </form>
  )
}

// -- Quality Judgement (Slice 5) --

function QualityPanel({ swarm, onError }) {
  const [quality, setQuality] = useState(null)   // {definition_of_ready, definition_of_done} | null
  const [dorGuidance, setDorGuidance] = useState('')
  const [dodGuidance, setDodGuidance] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!swarm) return
    setQuality(null)
    api.getQuality(swarm.id)
      .then(q => { if (q && !q.detail) setQuality(q) })
      .catch(() => {})   // no standards yet — panel just shows the generate form
  }, [swarm])

  async function generate(e) {
    e.preventDefault()
    setBusy(true)
    try {
      const q = await api.generateQuality(swarm.id, {
        dor_guidance: dorGuidance.trim(),
        dod_guidance: dodGuidance.trim(),
      })
      if (q && !q.detail) {
        setQuality(q)
        setDorGuidance(''); setDodGuidance('')
      } else {
        onError(q?.detail || 'Quality generation failed')
      }
    } catch (err) {
      onError(explain('Generating quality standards', err))
    } finally {
      setBusy(false)
    }
  }

  const hasStandards = quality && (quality.definition_of_ready || quality.definition_of_done)

  return (
    <div className="card">
      <h3 style={{marginTop: 0}}>Quality Standards</h3>
      {hasStandards ? (
        <div style={{display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, fontSize: 14, whiteSpace: 'pre-wrap'}}>
          <div>
            <strong>Definition of Ready</strong>
            <p style={{margin: '6px 0 0'}}>{quality.definition_of_ready || '—'}</p>
          </div>
          <div>
            <strong>Definition of Done</strong>
            <p style={{margin: '6px 0 0'}}>{quality.definition_of_done || '—'}</p>
          </div>
        </div>
      ) : (
        <p style={{color: '#666', fontSize: 14}}>No quality standards yet — generate a Definition of Ready / Done from this swarm's vision and mission.</p>
      )}

      <form onSubmit={generate} style={{marginTop: 12}}>
        <div style={{display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8}}>
          <label style={{fontSize: 13}}>
            Guidance for Definition of Ready (optional)
            <textarea value={dorGuidance} onChange={(e) => setDorGuidance(e.target.value)} rows={2} placeholder="e.g. every task must name its verifier" />
          </label>
          <label style={{fontSize: 13}}>
            Guidance for Definition of Done (optional)
            <textarea value={dodGuidance} onChange={(e) => setDodGuidance(e.target.value)} rows={2} placeholder="e.g. tests run in Docker, no local venvs" />
          </label>
        </div>
        <button type="submit" disabled={busy} style={{marginTop: 8}}>
          {busy ? 'Judging…' : hasStandards ? '↻ Regenerate standards' : '✨ Generate quality standards'}
        </button>
      </form>
    </div>
  )
}

// -- Swarm detail --

function SwarmDetail({ swarm, onRefresh, onError }) {
  const [tasks, setTasks] = useState([])
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!swarm) return
    api.listTasks(swarm.id).then(res => {
      const tasks = Array.isArray(res) ? res : (res.tasks || [])
      setTasks(tasks)
    }).catch(() => onError('Fetching tasks'))
  }, [swarm, onError])

  if (!swarm) return null

  const actions = ALLOWED_ACTIONS[swarm.lifecycle_state] || []
  const status = STATUSES.includes(swarm.lifecycle_state) ? swarm.lifecycle_state : 'In-Progress'

  async function doAction(action) {
    try {
      await api.transition(swarm.id, action)
      onRefresh()
    } catch (err) {
      onError(explain(`Transition ${action}`, err))
    }
  }

  async function handleDelete() {
    if (!window.confirm(`Hard-delete swarm "${swarm.name}"? All tasks will be lost.`)) return
    try {
      await api.deleteSwarm(swarm.id)
      onRefresh()
    } catch (err) {
      onError(explain('Deleting swarm', err))
    }
  }

  return (
    <div>
      <div className="card">
        <div className="section-header">
          <div>
            <h2 style={{margin: 0}}>{swarm.name}</h2>
            <div style={{display: 'flex', gap: 8, marginTop: 6, flexWrap: 'wrap'}}>
              <LifecycleBadge state={swarm.lifecycle_state} />
              <span className="chip">{status}</span>
              <span className="chip">ID: {shortId(swarm.id)}</span>
            </div>
          </div>
        </div>
        <div style={{marginTop: 12, color: '#666', fontSize: 14}}>
          <p><strong>Vision:</strong> {swarm.vision_statement}</p>
          {swarm.mission_statement && <p><strong>Mission:</strong> {swarm.mission_statement}</p>}
        </div>
        <div style={{display: 'flex', gap: 8, marginTop: 16, flexWrap: 'wrap'}}>
          {actions.map(a => (
            <button key={a.action} onClick={() => doAction(a.action)}>{a.label}</button>
          ))}
          {actions.some(a => a.action === 'delete') && (
            <button onClick={handleDelete} style={{background: '#d9534f', color: '#fff'}}>🗑 Hard Delete</button>
          )}
        </div>
      </div>

      <QualityPanel swarm={swarm} onError={onError} />

      <div style={{marginBottom: 24}}>
        <TaskForm teamId={swarm.id} onCreated={(t) => setTasks(prev => [t, ...prev])} onError={onError} />

        <h3>Task Board</h3>
        <div className="board">
          {STATUSES.map(s => (
            <div key={s} className="column">
              <h4>{s} ({tasks.filter(t => t.status === s).length})</h4>
              {tasks.filter(t => t.status === s).map(task => (
                <TaskItem key={task.id} task={task} onUpdated={(t) => setTasks(prev => prev.map(x => x.id === t.id ? t : x))} onError={onError} />
              ))}
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

// -- Archived list --

function ArchivedSwarmList({ onRestore }) {
  const [swarms, setSwarms] = useState([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    api.listTeams()
      .then(res => {
        const teams = Array.isArray(res) ? res : (res.teams || [])
        const archived = teams.filter(t => t.lifecycle_state === 'ARCHIVED')
        setSwarms(archived)
        setLoading(false)
      })
      .catch(() => setLoading(false))
  }, [])

  if (loading || swarms.length === 0) return null

  return (
    <div className="card">
      <h2>Archived swarms</h2>
      {swarms.length === 0 ? (
        <p>No archived swarms.</p>
      ) : (
        <ul className="list-group">
          {swarms.map(s => (
            <li key={s.id} className="list-group-item" style={{display: 'flex', justifyContent: 'space-between'}}>
              <div>
                <strong>{s.name}</strong>
                <div style={{fontSize: 12, color: '#666'}}>{shortId(s.id)}</div>
              </div>
              <LifecycleBadge state={s.lifecycle_state} />
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

// -- Task components --

function TaskForm({ teamId, onCreated, onError }) {
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [criteria, setCriteria] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit(e) {
    e.preventDefault()
    if (!name.trim()) return
    setBusy(true)
    try {
      const task = await api.createTask({ team_id: teamId, name, description, acceptance_criteria: criteria })
      setName(''); setDescription(''); setCriteria('')
      onCreated(task)
    } catch (err) {
      onError(explain('Creating task', err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="card form" onSubmit={submit}>
      <h3>New task</h3>
      <label>
        Name
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Implement feature X" required />
      </label>
      <label>
        Description
        <textarea value={description} onChange={(e) => setDescription(e.target.value)} placeholder="Step-by-step instructions…" rows={3} />
      </label>
      <label>
        Acceptance criteria
        <input value={criteria} onChange={(e) => setCriteria(e.target.value)} placeholder="Given… When… Then…" />
      </label>
      <button type="submit" disabled={busy || !name.trim()}>{busy ? 'Creating…' : 'Add task'}</button>
    </form>
  )
}

function TaskItem({ task, onUpdated, onError }) {
  const [showReject, setShowReject] = useState(false)
  const [rejectionReason, setRejectionReason] = useState('')

  async function advance() {
    const map = { 'Pending': 'Ready', 'Ready': 'In-Progress', 'In-Progress': 'Done' }
    const next = map[task.status]
    if (!next) return
    try {
      await api.updateTask(task.id, { status: next })
      onUpdated({ ...task, status: next })
    } catch (err) {
      onError(explain('Updating task status', err))
    }
  }

  async function reject() {
    if (!rejectionReason.trim()) return
    try {
      await api.updateTask(task.id, { status: 'Ready', rejection_reason: rejectionReason.trim() })
      onUpdated({ ...task, status: 'Ready', rejection_reason: rejectionReason.trim() })
      setShowReject(false)
      setRejectionReason('')
    } catch (err) {
      onError(explain('Rejecting task', err))
    }
  }

  async function handleDelete() {
    try {
      await api.deleteTask(task.id)
      onUpdated({ ...task, status: 'GONE' })
    } catch (err) {
      onError(explain('Deleting task', err))
    }
  }

  if (task.status === 'GONE') return null

  return (
    <div className="task-item" style={{border: '1px solid #ccc', borderRadius: 8, padding: 12, marginBottom: 8, background: task.status === 'Done' ? '#f0f0f0' : '#fff'}}>
      <div style={{display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start'}}>
        <div style={{flex: 1}}>
          <strong>{task.name}</strong>
          <div style={{fontSize: 12, color: '#666'}}>{shortId(task.id)}</div>
          {task.description && <p style={{margin: '4px 0'}}>{task.description}</p>}
          {task.acceptance_criteria && <p style={{margin: '4px 0', fontSize: 13, color: '#444'}}><strong>AC:</strong> {task.acceptance_criteria}</p>}
          {task.rejection_reason && <p style={{margin: '4px 0', fontSize: 13, color: '#d9534f'}}><strong>Rejected:</strong> {task.rejection_reason}</p>}
        </div>
        <div style={{display: 'flex', gap: 4, flexShrink: 0, marginLeft: 8}}>
          {task.status !== 'Done' && (
            <>
              <button onClick={advance} style={{background: '#5cb85c', color: '#fff'}}>→</button>
              {task.status === 'Ready' && (
                <button onClick={() => setShowReject(true)} style={{background: '#d9534f', color: '#fff'}}>✎</button>
              )}
            </>
          )}
          <button onClick={handleDelete} style={{background: '#f0ad4e', color: '#fff'}}>🗑</button>
        </div>
      </div>
      {showReject && (
        <div style={{marginTop: 8, display: 'flex', gap: 4}}>
          <input
            value={rejectionReason}
            onChange={(e) => setRejectionReason(e.target.value)}
            placeholder="Rejection reason"
            autoFocus
            style={{flex: 1, padding: 4}}
          />
          <button onClick={reject} style={{background: '#d9534f', color: '#fff', padding: '4px 8px'}}>Reject</button>
          <button onClick={() => setShowReject(false)} style={{padding: '4px 8px'}}>Cancel</button>
        </div>
      )}
    </div>
  )
}

// -- Helpers --

function explain(action, err) {
  return typeof err === 'string'
    ? `${action}: ${err}`
    : err?.detail
      ? `${action}: ${err.detail}`
      : `${action} failed`
}

// -- Main App --

export default function App() {
  const [swarm, setSwarm] = useState(null)       // single live swarm
  const [swarms, setSwarms] = useState([])        // full history
  const [errorMsg, setErrorMsg] = useState('')
  const [tab, setTab] = useState('active')        // active | archived

  // Load active swarm + full history on mount (and refreshes)
  useEffect(() => {
    api.getActiveSwarm()
      .then(s => setSwarm(s))
      .catch(() => setSwarm(null))
    api.listTeams()
      .then(res => setSwarms(Array.isArray(res) ? res : (res.teams || [])))
      .catch(() => setSwarms([]))
  }, [])

  function refresh() {
    api.getActiveSwarm()
      .then(s => setSwarm(s))
      .catch(() => setSwarm(null))
    api.listTeams()
      .then(res => setSwarms(Array.isArray(res) ? res : (res.teams || [])))
      .catch(() => setSwarms([]))
  }

  return (
    <div className="container">
      <h1>DAASH</h1>
      {errorMsg && <div className="alert">{errorMsg}<button onClick={() => setErrorMsg('')}>×</button></div>}
      <div className="sidebar">
        <nav>
          <ul>
            <li><strong>Single Swarm Orchestrator</strong></li>
            {swarm && (
              <li>
                <div className="swarm-status">
                  <strong>{swarm.name}</strong>
                  <LifecycleBadge state={swarm.lifecycle_state} />
                </div>
              </li>
            )}
            <li style={{borderTop: '1px solid #eee', paddingTop: 8}}>
              <button onClick={() => setTab('active')} style={{fontWeight: tab === 'active' ? 'bold' : 'normal'}}>
                {swarm ? 'Active Swarm' : 'No Active Swarm'}
              </button>
            </li>
            <li>
              <button onClick={() => setTab('archived')} style={{fontWeight: tab === 'archived' ? 'bold' : 'normal'}}>
                Archived ({swarms.filter(t => t.lifecycle_state === 'ARCHIVED').length})
              </button>
            </li>
          </ul>
        </nav>
      </div>

      <main>
        {tab === 'active' && (
          swarm
            ? <SwarmDetail swarm={swarm} onRefresh={refresh} onError={setErrorMsg} />
            : <div>
                <p>No active swarm. Create one below.</p>
                <SwarmForm onCreated={refresh} onError={setErrorMsg} />
              </div>
        )}

        {tab === 'archived' && (
          <ArchivedSwarmList />
        )}
      </main>
    </div>
  )
}
