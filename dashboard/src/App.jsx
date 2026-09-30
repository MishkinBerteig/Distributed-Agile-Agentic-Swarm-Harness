import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from './api.js'

const STATUSES = ['Pending', 'Ready', 'In-Progress', 'Done']

function shortId(id) {
  return id ? id.slice(0, 8) : ''
}

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
      onError(String(err.message || err))
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
      // Embeddings are computed server-side (gte-base-en-v1.5) on creation.
      await api.createTask({ team_id: teamId, name: name.trim(), description, acceptance_criteria: criteria })
      setName(''); setDescription(''); setCriteria('')
      onCreated()
    } catch (err) {
      onError(String(err.message || err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="card form" onSubmit={submit}>
      <h2>Add task</h2>
      <label>
        Name
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Implement retry logic" required />
      </label>
      <label>
        Description
        <textarea value={description} onChange={(e) => setDescription(e.target.value)} rows={2} />
      </label>
      <label>
        Acceptance criteria
        <textarea value={criteria} onChange={(e) => setCriteria(e.target.value)} rows={2} />
      </label>
      <button type="submit" disabled={busy || !name.trim()}>{busy ? 'Creating…' : 'Add task'}</button>
    </form>
  )
}

function TaskTree({ tasks }) {
  const byParent = useMemo(() => {
    const map = new Map()
    for (const t of tasks) {
      const key = t.parent_id ?? ''
      if (!map.has(key)) map.set(key, [])
      map.get(key).push(t)
    }
    return map
  }, [tasks])

  function renderNode(task, depth) {
    const children = byParent.get(task.id) ?? []
    return (
      <li key={task.id}>
        <div className="task-row" style={{ marginLeft: depth * 20 }}>
          <span className={`badge ${task.status.toLowerCase()}`}>{task.status}</span>
          <span className="task-name">{task.name}</span>
          <span className="task-id" title={task.id}>{shortId(task.id)}</span>
        </div>
        {task.description && <div className="task-desc" style={{ marginLeft: depth * 20 + 24 }}>{task.description}</div>}
        {!!task.acceptance_criteria && (
          <div className="task-criteria" style={{ marginLeft: depth * 20 + 24 }}>✓ {task.acceptance_criteria}</div>
        )}
        {children.length > 0 && <ul>{children.map((c) => renderNode(c, depth + 1))}</ul>}
      </li>
    )
  }

  const roots = byParent.get('') ?? []
  if (tasks.length === 0) return <p className="empty">No tasks yet in this swarm.</p>
  return <ul className="task-tree">{roots.map((t) => renderNode(t, 0))}</ul>
}

export default function App() {
  const [teams, setTeams] = useState([])
  const [selectedId, setSelectedId] = useState(null)
  const [tasks, setTasks] = useState([])
  const [statusFilter, setStatusFilter] = useState('')
  const [error, setError] = useState(null)

  const refreshTeams = useCallback(async () => {
    try {
      const list = await api.listTeams()
      setTeams(list)
      setSelectedId((cur) => cur ?? (list[0]?.id ?? null))
    } catch (err) {
      setError(`Failed to load swarms: ${err.message}`)
    }
  }, [])

  const refreshTasks = useCallback(async () => {
    if (!selectedId) { setTasks([]); return }
    try {
      setTasks(await api.listTasks(selectedId, statusFilter))
    } catch (err) {
      setError(`Failed to load tasks: ${err.message}`)
    }
  }, [selectedId, statusFilter])

  useEffect(() => { refreshTeams() }, [refreshTeams])
  useEffect(() => { refreshTasks() }, [refreshTasks])

  const selected = teams.find((t) => t.id === selectedId) ?? null

  return (
    <div className="layout">
      <header>
        <h1>DAASHboard</h1>
        <span className="sub">Distributed Agile Agentic Swarm Harness</span>
      </header>
      {error && (
        <div className="error" role="alert">
          {error} <button onClick={() => setError(null)}>✕</button>
        </div>
      )}
      <main>
        <section className="col">
          <div className="card">
            <h2>Swarms</h2>
            {teams.length === 0 && <p className="empty">No swarms yet — create one below.</p>}
            <ul className="swarm-list">
              {teams.map((t) => (
                <li key={t.id}>
                  <button
                    className={`swarm-item ${t.id === selectedId ? 'active' : ''}`}
                    onClick={() => setSelectedId(t.id)}
                  >
                    <span className="swarm-name">{t.name}</span>
                    <span className="task-id" title={t.id}>{shortId(t.id)}</span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
          <SwarmForm onCreated={refreshTeams} onError={setError} />
        </section>

        <section className="col wide">
          {selected ? (
            <>
              <div className="card">
                <div className="team-header">
                  <h2>{selected.name}</h2>
                  <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
                    <option value="">All statuses</option>
                    {STATUSES.map((s) => <option key={s} value={s}>{s}</option>)}
                  </select>
                </div>
                {selected.vision_statement && <p className="vision">Vision: {selected.vision_statement}</p>}
                {selected.mission_statement && <p className="vision">Mission: {selected.mission_statement}</p>}
                <TaskTree tasks={tasks} />
              </div>
              <TaskForm teamId={selected.id} onCreated={refreshTasks} onError={setError} />
            </>
          ) : (
            <div className="card"><p className="empty">Select or create a swarm to inspect its tasks.</p></div>
          )}
        </section>
      </main>
    </div>
  )
}
