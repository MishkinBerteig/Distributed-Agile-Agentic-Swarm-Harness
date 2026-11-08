// Pure helpers shared by components.

// Mirrors SWARM_ACTIONS in app/models.py (action -> {from_state: to_state}).
export const SWARM_ACTIONS = {
  start: { CREATED: 'ACTIVE' },
  pause: { ACTIVE: 'PAUSED', USER_FEEDBACK: 'PAUSED' },
  resume: { PAUSED: 'ACTIVE', USER_FEEDBACK: 'ACTIVE' },
  feedback: { PAUSED: 'USER_FEEDBACK' },
  verify: { ACTIVE: 'VERIFICATION', PAUSED: 'VERIFICATION' },
  learn: { VERIFICATION: 'LEARNING' },
  archive: { VERIFICATION: 'ARCHIVED', LEARNING: 'ARCHIVED' },
  delete: { CREATED: 'DELETED', VERIFICATION: 'DELETED' },
}
// `feedback` is submitted through the feedback textbox, not as a button.
export const actionsFor = (state) =>
  Object.keys(SWARM_ACTIONS).filter((a) => a !== 'feedback' && SWARM_ACTIONS[a][state])

export const ACTION_LABEL = {
  start: 'Start', pause: 'Pause', resume: 'Resume', verify: 'Verify',
  learn: 'Learn', archive: 'Archive', delete: 'Delete',
}

export const CHECK_KINDS = ['rubric', 'file_exists', 'file_contains', 'min_words', 'json_valid']
export const TASK_COLUMNS = ['Pending', 'Ready', 'In-Progress', 'Review']
export const TERMINAL_COLUMNS = ['Done', 'Failed', 'Rejected']

export const statusClass = (s) =>
  's-' + String(s || 'unknown').toLowerCase().replace(/[^a-z]+/g, '-')

export function humanDuration(sec) {
  if (sec === null || sec === undefined || Number.isNaN(sec)) return '-'
  sec = Math.round(sec)
  if (sec < 60) return `${sec}s`
  const m = Math.floor(sec / 60)
  if (m < 60) return `${m}m ${sec % 60}s`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ${m % 60}m`
  return `${Math.floor(h / 24)}d ${h % 24}h`
}

export const fmtNum = (n) => (n === null || n === undefined ? '-' : Number(n).toLocaleString())

export function fmtTime(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? String(iso) : d.toLocaleTimeString()
}
export function fmtDateTime(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? String(iso) : d.toLocaleString()
}

export function criteriaSummary(task) {
  const c = task.criteria || []
  const green = c.filter((x) => x.status === 'green').length
  const red = c.filter((x) => x.status === 'red').length
  return { total: c.length, green, red, unknown: c.length - green - red }
}

export function creatorLabel(team, teamsById) {
  if (team.creator_kind === 'user') return 'user'
  if (team.creator_kind === 'swarm_coordinator') return 'swarm coordinator'
  const parent = teamsById && teamsById[team.creator_team_id]
  return `team ${parent ? parent.name : team.creator_team_id || '?'}`
}

// Lookup of actor id -> display name from the board.
export function actorNames(board) {
  const map = { user: 'user', engine: 'engine' }
  if (!board) return map
  if (board.coordinator) map[board.coordinator.id] = board.coordinator.name
  for (const t of board.teams || []) for (const a of t.agents || []) map[a.id] = a.name
  return map
}

export function eventText(ev) {
  const move = ev.from_status || ev.to_status
    ? ` (${ev.from_status || '?'} -> ${ev.to_status || '?'})` : ''
  return `${ev.detail || ev.kind}${move}`
}

export function buildArtifactTree(files) {
  const root = { children: {}, files: [] }
  for (const f of files || []) {
    const parts = f.path.split('/')
    let node = root
    for (const p of parts.slice(0, -1)) {
      node.children[p] = node.children[p] || { children: {}, files: [] }
      node = node.children[p]
    }
    node.files.push({ ...f, name: parts[parts.length - 1] })
  }
  return root
}

// Number input value -> number or undefined (blank).
export const num = (v) => (v === '' || v === null || v === undefined ? undefined : Number(v))
