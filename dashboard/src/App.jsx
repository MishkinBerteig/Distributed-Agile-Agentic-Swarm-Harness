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
      onError(explain('Creating task', err))
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

function DecisionBus({ signals, proposals, proposalDetail, proposalForm, voterId, onCreateProposal, onVote, onOpenProposal, onSetVoterId, onSetProposalForm, onError, refreshSignals, refreshProposals }) {
  const statusColor = (s) => {
    if (s === 'approved') return '#22c55e'
    if (s === 'rejected') return '#ef4444'
    if (s === 'pending') return '#f59e0b'
    return '#6b7280'
  }

  return (
    <div>
      <div style={{ display: 'flex', gap: 16, marginBottom: 16, flexWrap: 'wrap' }}>
        {/* Signal Feed */}
        <div className="card" style={{ flex: 1, minWidth: 280 }}>
          <h2>Signal Feed <button onClick={refreshSignals} style={{ float: 'right', fontSize: 12, cursor: 'pointer' }}>↻</button></h2>
          {signals.length === 0 && <p className="empty">No signals yet.</p>}
          <ul style={{ listStyle: 'none', padding: 0, margin: 0 }}>
            {signals.map((s) => (
              <li key={s.id} style={{ padding: '8px 0', borderBottom: '1px solid #eee' }}>
                <span className="badge" style={{ backgroundColor: statusColor(s.kind === 'proposal_approved' ? 'approved' : s.kind === 'proposal_rejected' ? 'rejected' : 'pending'), marginRight: 8 }}>{s.kind.replace('proposal_', '')}</span>
                <span>{s.payload?.reason || s.kind}</span>
                {s.team_id && <span className="task-id" style={{ marginLeft: 8 }} title={s.team_id}>{shortId(s.team_id)}</span>}
                <div style={{ fontSize: 11, color: '#999', marginTop: 2 }}>{new Date(s.created_at).toLocaleString()}</div>
              </li>
            ))}
          </ul>
        </div>

        {/* Proposals List */}
        <div className="card" style={{ flex: 1, minWidth: 280 }}>
          <h2>Proposals <button onClick={refreshProposals} style={{ float: 'right', fontSize: 12, cursor: 'pointer' }}>↻</button></h2>
          {proposals.length === 0 && <p className="empty">No proposals yet.</p>}
          <ul style={{ listStyle: 'none', padding: 0, margin: 0 }}>
            {proposals.map((p) => (
              <li key={p.id} style={{ padding: '8px 0', borderBottom: '1px solid #eee' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                  <span className="badge" style={{ backgroundColor: statusColor(p.status), marginRight: 8 }}>{p.status}</span>
                  <strong style={{ cursor: 'pointer', color: '#1d4ed8' }} onClick={() => onOpenProposal(p.id)}>{p.question}</strong>
                </div>
                <div style={{ fontSize: 12, marginTop: 4 }}>
                  {p.approve_count} approve · {p.reject_count} reject · quorum: {p.quorum}
                  {p.task_id && <span className="task-id" style={{ marginLeft: 8 }} title={p.task_id}>{shortId(p.task_id)}</span>}
                </div>
                {p.status !== 'pending' && p.decided_at && (
                  <div style={{ fontSize: 11, color: '#999', marginTop: 2 }}>Decided {new Date(p.decided_at).toLocaleString()}</div>
                )}
              </li>
            ))}
          </ul>
        </div>
      </div>

      {/* Create Proposal + Voting */}
      <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
        <div className="card form" style={{ flex: 1, minWidth: 280 }}>
          <h2>Create Proposal</h2>
          <form onSubmit={onCreateProposal}>
            <label>
              Team ID
              <input value={proposalForm.team_id} onChange={(e) => onSetProposalForm({ team_id: e.target.value })} placeholder="team-id" required />
            </label>
            <label>
              Question
              <input value={proposalForm.question} onChange={(e) => onSetProposalForm({ question: e.target.value })} placeholder="Approve the release?" required />
            </label>
            <label>
              Quorum
              <input type="number" min="1" value={proposalForm.quorum} onChange={(e) => onSetProposalForm({ quorum: parseInt(e.target.value) || 1 })} />
            </label>
            <button type="submit">Create</button>
          </form>
        </div>

        <div className="card" style={{ flex: 1, minWidth: 280 }}>
          <h2>Voting Booth</h2>
          <label>
            Voter ID
            <input value={voterId} onChange={(e) => onSetVoterId(e.target.value)} placeholder="user-1" />
          </label>
          {proposalDetail ? (
            <div>
              <p><strong>{proposalDetail.question}</strong></p>
              <p>Result: <span className="badge" style={{ backgroundColor: statusColor(proposalDetail.status) }}>{proposalDetail.status}</span> ({proposalDetail.approve_count} approve / {proposalDetail.reject_count} reject)</p>
              {proposalDetail.status === 'pending' && (
                <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
                  <button onClick={() => onVote(proposalDetail.id, true)} style={{ backgroundColor: '#22c55e', color: '#fff', border: 'none', padding: '8px 16px', borderRadius: 4, cursor: 'pointer' }}>Approve</button>
                  <button onClick={() => onVote(proposalDetail.id, false)} style={{ backgroundColor: '#ef4444', color: '#fff', border: 'none', padding: '8px 16px', borderRadius: 4, cursor: 'pointer' }}>Reject</button>
                </div>
              )}
            </div>
          ) : (
            <p className="empty">Create or open a proposal to vote.</p>
          )}
        </div>
      </div>
    </div>
  )
}

// Turn a failed API call into a plain-language message with an explanation
// of what likely went wrong and what to do about it.
function explain(action, err) {
  const detail = err?.message ?? String(err)
  let why = 'The coordinator rejected the request.'
  if (/Failed to fetch|NetworkError/i.test(detail)) {
    why = 'The board could not reach the coordinator — is `docker compose up` running and healthy?'
  } else if (/\b404\b|not found/i.test(detail)) {
    why = 'It no longer exists on the coordinator — proposal state lives in Redis and expires, so refresh the list.'
  } else if (/already decided/i.test(detail)) {
    why = 'This proposal already reached quorum, so further votes are refused. Refresh to see the outcome.'
  } else if (/already voted|duplicate/i.test(detail)) {
    why = "That voter has already cast a ballot on this proposal — each voter may vote once. Try a different Voter ID."
  } else if (/\b422\b|validation/i.test(detail)) {
    why = 'The request was missing or had invalid fields — check the form values.'
  }
  return `${action} failed: ${detail}. ${why}`
}

export default function App() {
  const [teams, setTeams] = useState([])
  const [selectedId, setSelectedId] = useState(null)
  const [tasks, setTasks] = useState([])
  const [statusFilter, setStatusFilter] = useState('')
  const [error, setError] = useState(null)

  // Decision Bus state (Slice 2)
  const [activeTab, setActiveTab] = useState('overview')
  const [signals, setSignals] = useState([])
  const [proposals, setProposals] = useState([])
  const [proposalDetail, setProposalDetail] = useState(null)
  const [proposalForm, setProposalForm] = useState({
    team_id: '',
    question: '',
    quorum: 1,
  })
  const [voterId, setVoterId] = useState('user-1')

  const refreshTeams = useCallback(async () => {
    try {
      const list = await api.listTeams()
      setTeams(list)
      setSelectedId((cur) => cur ?? (list[0]?.id ?? null))
    } catch (err) {
      setError(explain('Loading swarms', err))
    }
  }, [])

  const refreshTasks = useCallback(async () => {
    if (!selectedId) { setTasks([]); return }
    try {
      setTasks(await api.listTasks(selectedId, statusFilter))
    } catch (err) {
      setError(explain('Loading tasks', err))
    }
  }, [selectedId, statusFilter])

  useEffect(() => { refreshTeams() }, [refreshTeams])
  useEffect(() => { refreshTasks() }, [refreshTasks])

  // Decision Bus (Slice 2)
  const refreshSignals = useCallback(async () => {
    try { setSignals(await api.listDecisions()) } catch (err) { setError(explain('Loading decision signals', err)) }
  }, [])
  const refreshProposals = useCallback(async () => {
    try { setProposals(await api.listProposals()) } catch (err) { setError(explain('Loading proposals', err)) }
  }, [])
  useEffect(() => { refreshSignals() }, [refreshSignals])
  useEffect(() => { refreshProposals() }, [refreshProposals])

  const handleCreateProposal = useCallback(async (e) => {
    e.preventDefault()
    try {
      const p = await api.createProposal(proposalForm)
      setProposals((prev) => [p, ...prev])
      setProposalForm({ team_id: '', question: '', quorum: 1 })
      refreshProposals()
    } catch (err) { setError(explain('Creating proposal', err)) }
  }, [proposalForm, refreshProposals])

  const handleVote = useCallback(async (proposalId, approve) => {
    try {
      const p = await api.castVote(proposalId, { voter: voterId, approve })
      setProposalDetail(p)
      refreshProposals()
      refreshSignals()
    } catch (err) { setError(explain(`Voting as "${voterId}"`, err)) }
  }, [voterId, refreshProposals, refreshSignals])

  const handleOpenProposal = useCallback(async (id) => {
    try {
      const p = await api.getProposal(id)
      setProposalDetail(p)
    } catch (err) { setError(explain('Opening proposal', err)) }
  }, [])

  const selected = teams.find((t) => t.id === selectedId) ?? null

  return (
    <div className="layout">
      <header>
        <h1>DAASHboard</h1>
        <span className="sub">Distributed Agile Agentic Swarm Harness</span>
      </header>
      <nav className="tabs">
        <button className={activeTab === 'overview' ? 'active' : ''} onClick={() => setActiveTab('overview')}>Overview</button>
        <button className={activeTab === 'decisions' ? 'active' : ''} onClick={() => { setActiveTab('decisions'); refreshSignals(); refreshProposals() }}>Decision Bus</button>
      </nav>
      {error && (
        <div className="error" role="alert">
          {error} <button onClick={() => setError(null)}>✕</button>
        </div>
      )}
      <main>
        {activeTab === 'overview' && (
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
        )}

        {activeTab === 'overview' && (
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
        )}

        {activeTab === 'decisions' && (
        <section className="col wide">
          <DecisionBus
            signals={signals}
            proposals={proposals}
            proposalDetail={proposalDetail}
            proposalForm={proposalForm}
            voterId={voterId}
            onCreateProposal={handleCreateProposal}
            onVote={handleVote}
            onOpenProposal={handleOpenProposal}
            onSetVoterId={(v) => setVoterId(v)}
            onSetProposalForm={(fn) => setProposalForm((f) => ({ ...f, ...fn(typeof fn === 'function' ? fn(proposalForm) : fn) }))}
            onError={setError}
            refreshSignals={refreshSignals}
            refreshProposals={refreshProposals}
          />
        </section>
        )}
      </main>
    </div>
  )
}
