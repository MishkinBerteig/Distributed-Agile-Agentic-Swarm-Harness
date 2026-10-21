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

// -- Agent session roster (Slice 5.1) --

const SESSION_COLORS = {
  live:       '#5cb85c',
  launching:  '#f0ad4e',
  failed:     '#d9534f',
  stopped:    '#95a5a6',
  pending:    '#bdc3c7',
}

function RosterPanel({ swarm, onRefresh }) {
  const [roster, setRoster] = useState(null)
  const [pollError, setPollError] = useState(false)

  // Poll while the swarm is live so the roster tracks real agent sessions.
  useEffect(() => {
    if (!swarm || swarm.lifecycle_state !== 'ACTIVE') {
      setRoster(null)
      return
    }
    let cancelled = false
    async function tick() {
      try {
        const res = await api.getSwarmSessions(swarm.id)
        if (!cancelled) {
          setRoster(res)
          setPollError(false)
        }
      } catch {
        if (!cancelled) setPollError(true)
      }
    }
    tick()
    const timer = setInterval(tick, 3000)
    return () => { cancelled = true; clearInterval(timer) }
  }, [swarm?.id, swarm?.lifecycle_state])

  async function addTeam(e) {
    e.preventDefault()
    const form = new FormData(e.currentTarget)
    const name = form.get('name')?.trim()
    const mission = form.get('mission')?.trim() || ''
    if (!name) return
    try {
      await api.createTeam({ swarm_id: swarm.id, name, mission_statement: mission })
      onRefresh()
      e.currentTarget.reset()
    } catch (err) {
      setPollError(true)
    }
  }

  const runnerDown = roster
    ? roster.teams.every(g => g.sessions.every(s => s.session_state === 'failed' && /runner/i.test(s.error || '')))
    : false

  return (
    <div className="card">
      <h3 style={{marginTop: 0}}>Agent Roster</h3>
      {swarm.lifecycle_state !== 'ACTIVE' ? (
        <p style={{color: '#666', fontSize: 14}}>
          Agents run while the swarm is live — press ▶ Start to launch every team member as a real harness session.
        </p>
      ) : roster && runnerDown ? (
        <p style={{color: '#d9534f', fontSize: 14}}>
          ⚠ Agent runner unreachable — no sessions launched. On the device where OpenClaude is installed,
          run <code>DAASH_RUNNER_TOKEN=change-me-runner python3 -m app.runner</code> and press ▶ Start again.
        </p>
      ) : roster ? (
        <>
          {roster.teams.map(group => (
            <div key={group.team_id} style={{marginBottom: 14}}>
              <strong style={{fontSize: 14}}>{group.team_name || shortId(group.team_id)}</strong>
              <table style={{width: '100%', fontSize: 13, borderCollapse: 'collapse', marginTop: 4}}>
                <thead>
                  <tr style={{color: '#666', textAlign: 'left'}}>
                    <th style={{padding: '2px 8px 2px 0'}}>Role</th>
                    <th>Harness</th>
                    <th>Model</th>
                    <th>Status</th>
                    <th>Turns</th>
                  </tr>
                </thead>
                <tbody>
                  {group.sessions.map(s => (
                    <tr key={s.id} style={{verticalAlign: 'top'}}>
                      <td style={{padding: '2px 8px 2px 0'}}>{s.role}</td>
                      <td>{s.harness}</td>
                      <td>{s.model || '—'}</td>
                      <td>
                        <span className="chip" style={{background: SESSION_COLORS[s.session_state] || '#999', color: '#fff'}}>
                          {s.session_state}
                        </span>
                      </td>
                      <td>{s.turns ?? 0}</td>
                    </tr>
                  ))}
                  {group.sessions.filter(s => s.error).map(s => (
                    <tr key={s.id + '-err'}>
                      <td colSpan="5" style={{color: '#d9534f', fontSize: 12, padding: '0 0 4px 8px'}}>
                        {s.role}: {s.error}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ))}
          {roster.teams.length === 0 && <p style={{color: '#666', fontSize: 14}}>No teams in this swarm yet.</p>}
        </>
      ) : (
        <p style={{color: '#666', fontSize: 14}}>Loading roster…</p>
      )}
      {pollError && !runnerDown && <p style={{color: '#d9534f', fontSize: 12}}>Roster refresh failed.</p>}

      <form onSubmit={addTeam} style={{marginTop: 10, display: 'flex', gap: 8, alignItems: 'end', flexWrap: 'wrap'}}>
        <label style={{fontSize: 13}}>
          Add team
          <input name="name" placeholder="Team name" style={{display: 'block', marginTop: 4}} required />
        </label>
        <label style={{fontSize: 13}}>
          Mission
          <input name="mission" placeholder="Team mission (optional)" style={{display: 'block', marginTop: 4, width: 220}} />
        </label>
        <button type="submit">+ Add team</button>
      </form>
    </div>
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
      // Vision is swarm-level; the mission seeds the swarm's first team.
      await api.createSwarm({
        name: name.trim(),
        vision_statement: vision,
        teams: [{ name: name.trim(), mission_statement: mission }],
      })
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
        Vision statement (swarm-level, flows into every session)
        <input value={vision} onChange={(e) => setVision(e.target.value)} placeholder="Ship verified value daily" />
      </label>
      <label>
        First team's mission
        <input value={mission} onChange={(e) => setMission(e.target.value)} placeholder="Automate the boring parts" />
      </label>
      <button type="submit" disabled={busy || !name.trim()}>{busy ? 'Creating…' : 'Create swarm'}</button>
    </form>
  )
}

// -- Quality Judgement (Slice 5) — per team --

function QualityPanel({ team, onError }) {
  const [quality, setQuality] = useState(null)   // {definition_of_ready, definition_of_done} | null
  const [dorGuidance, setDorGuidance] = useState('')
  const [dodGuidance, setDodGuidance] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!team) return
    setQuality(null)
    api.getQuality(team.id)
      .then(q => { if (q && !q.detail) setQuality(q) })
      .catch(() => {})   // no standards yet — panel just shows the generate form
  }, [team?.id])

  async function generate(e) {
    e.preventDefault()
    setBusy(true)
    try {
      const q = await api.generateQuality(team.id, {
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
      <h3 style={{marginTop: 0}}>Quality Standards — {team.name}</h3>
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
        <p style={{color: '#666', fontSize: 14}}>No quality standards yet — generate a Definition of Ready / Done from the swarm's vision and this team's mission.</p>
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

// -- Task board for one team --

function TeamSection({ team, swarm, onRefresh }) {
  const [tasks, setTasks] = useState([])

  const loadTasks = useCallback(() => {
    api.listTasks(team.id).then(res => {
      const list = Array.isArray(res) ? res : (res.tasks || [])
      setTasks(list)
    }).catch(() => {})
  }, [team.id])

  useEffect(() => { loadTasks() }, [loadTasks])

  return (
    <div style={{marginTop: 20, borderTop: '2px solid #eee', paddingTop: 12}}>
      <div style={{display: 'flex', alignItems: 'baseline', gap: 8, flexWrap: 'wrap'}}>
        <h3 style={{margin: 0}}>{team.name}</h3>
        {team.mission_statement && (
          <span style={{color: '#666', fontSize: 13}}>Mission: {team.mission_statement}</span>
        )}
      </div>

      <QualityPanel team={team} onError={() => {}} />

      <div>
        <TaskForm teamId={team.id} onCreated={(t) => setTasks(prev => [t, ...prev])} onStatusChange={loadTasks} />
        <h3 style={{marginTop: 12}}>Task Board — {team.name}</h3>
        <div className="board">
          {STATUSES.map(s => (
            <div key={s} className="column">
              <h4>{s} ({tasks.filter(t => t.status === s).length})</h4>
              {tasks.filter(t => t.status === s).map(task => (
                <TaskItem key={task.id} task={task} onUpdated={(t) => setTasks(prev => prev.map(x => x.id === t.id ? t : x))} />
              ))}
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

// -- Swarm detail --

function SwarmDetail({ swarm, onRefresh, onError }) {
  const actions = ALLOWED_ACTIONS[swarm.lifecycle_state] || []
  const teams = swarm.teams || []

  async function doAction(action) {
    try {
      await api.transition(swarm.id, action)
      onRefresh()
    } catch (err) {
      onError(explain(`Transition ${action}`, err))
    }
  }

  async function handleDelete() {
    if (!window.confirm(`Hard-delete swarm "${swarm.name}"? All teams and tasks will be lost.`)) return
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
              <span className="chip">{teams.length} team{teams.length === 1 ? '' : 's'}</span>
              <span className="chip">ID: {shortId(swarm.id)}</span>
            </div>
          </div>
        </div>
        <div style={{marginTop: 12, color: '#666', fontSize: 14}}>
          <p><strong>Vision:</strong> {swarm.vision_statement || '—'}</p>
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

      <RosterPanel swarm={swarm} onRefresh={onRefresh} />

      {teams.map(team => (
        <TeamSection key={team.id} team={team} swarm={swarm} onRefresh={onRefresh} />
      ))}
    </div>
  )
}

// -- Archived list --

function ArchivedSwarmList({ onRefresh }) {
  const [swarms, setSwarms] = useState([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    api.listSwarms()
      .then(res => {
        const list = Array.isArray(res) ? res : (res.swarms || [])
        setSwarms(list.filter(s => s.lifecycle_state === 'ARCHIVED'))
        setLoading(false)
      })
      .catch(() => setLoading(false))
  }, [])

  if (loading) return <p>Loading…</p>
  if (swarms.length === 0) return <p className="card">No archived swarms.</p>

  return (
    <div className="card">
      <h2>Archived swarms</h2>
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
    </div>
  )
}

// -- Task components --

function TaskForm({ teamId, onCreated, onStatusChange, onError = () => {} }) {
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

function TaskItem({ task, onUpdated, onError = () => {} }) {
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
  const [swarm, setSwarm] = useState(null)        // single live swarm
  const [swarms, setSwarms] = useState([])        // full history
  const [errorMsg, setErrorMsg] = useState('')
  const [tab, setTab] = useState('active')        // active | archived

  // Load active swarm + full history on mount (and refreshes)
  useEffect(() => {
    api.getActiveSwarm()
      .then(s => setSwarm(s))
      .catch(() => setSwarm(null))
    api.listSwarms()
      .then(res => setSwarms(Array.isArray(res) ? res : (res.swarms || [])))
      .catch(() => setSwarms([]))
  }, [])

  function refresh() {
    api.getActiveSwarm()
      .then(s => setSwarm(s))
      .catch(() => setSwarm(null))
    api.listSwarms()
      .then(res => setSwarms(Array.isArray(res) ? res : (res.swarms || [])))
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
                Archived ({swarms.filter(s => s.lifecycle_state === 'ARCHIVED').length})
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
          <ArchivedSwarmList onRefresh={refresh} />
        )}
      </main>
    </div>
  )
}
