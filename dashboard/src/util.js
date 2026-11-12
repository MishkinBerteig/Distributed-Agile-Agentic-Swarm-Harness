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

// "12s ago" style age from an ISO timestamp.
export function ago(iso) {
  if (!iso) return 'never'
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return String(iso)
  const sec = Math.max(0, (Date.now() - t) / 1000)
  return sec < 5 ? 'just now' : `${humanDuration(sec)} ago`
}

export const CAP_LABELS = { edit_files: 'edit files', run_code: 'run code', web_research: 'web research' }
export const capLabel = (c) => CAP_LABELS[c] || String(c).replace(/_/g, ' ')
export const OPTIONAL_CAPS = ['run_code', 'web_research']
export const HARNESS_DEFAULT_CAPS = {
  daash: ['edit_files'],
  openclaude: ['edit_files', 'run_code'],
  'claude-code': ['edit_files', 'run_code'],
  codex: ['edit_files', 'run_code'],
  openclaw: ['edit_files', 'run_code'],
  hermes: ['edit_files', 'run_code'],
}
export const defaultCaps = (h) => HARNESS_DEFAULT_CAPS[h] || ['edit_files']
export const sameCaps = (a, b) => [...a].sort().join() === [...b].sort().join()

export const HARNESS_CHOICES = ['daash', 'openclaude', 'claude-code', 'codex', 'openclaw', 'hermes']
// Display names (app/api_helpers.py HARNESS_LABELS), used until GET /availability answers.
export const HARNESS_NAMES = {
  daash: 'DAASH built-in agent', openclaude: 'OpenClaude', 'claude-code': 'Claude Code', codex: 'Codex',
  openclaw: 'OpenClaw', hermes: 'Hermes Agent', fake: 'Fake CLI (tests only)', connected: 'Connected agents',
}

// Why an agent harness is or is not available (GET /availability `status`).
export const HARNESS_STATUS = {
  available: { text: 'Available', on: true },
  offline: { text: 'Runner offline', on: false },
  disabled: { text: 'Runner disabled', on: false },
  no_runner: { text: 'No runner offers it', on: false },
  gateway_unreachable: { text: 'LLM gateway unreachable', on: false },
}
export const statusText = (s) => (HARNESS_STATUS[s] ? HARNESS_STATUS[s].text : String(s))
// Allowed harnesses that cannot run anything right now: their work waits, then falls back.
export const unavailableAllowed = (harnesses) =>
  (harnesses || []).filter((h) => h.allowed && h.status !== 'available')

// -- connected agents (DESIGN-0.4 section 5) ---------------------------------------------------

// A delivery row filled by a connected agent: {harness: 'connected', connected_agent: <id>}.
export const CONNECTED = 'connected'

// GET /connected-agents `availability`: a short status and what it means for a first-time user.
export const CONNECTED_STATUS = {
  available: { text: 'Available', tone: 'on',
    explain: 'The last check passed. It can take tasks.' },
  pending: { text: 'Waiting for approval', tone: 'wait',
    explain: 'Connected in DAASH, but its owner has not applied the approval step yet. DAASH checks by itself every few seconds.' },
  unchecked: { text: 'Not checked yet', tone: 'wait',
    explain: 'Approved, but DAASH has not tested it since. Use Test, or wait a minute.' },
  unreachable: { text: 'Unreachable', tone: 'off',
    explain: 'DAASH got no answer at its address. Its tasks wait and retry without using up attempts; after the swarm’s remote fallback time, DAASH’s built-in agent does them.' },
  token_rejected: { text: 'Token rejected', tone: 'off',
    explain: 'It refused DAASH’s token: the approval step was not applied, or was undone. Reset tokens to get a new step for its owner.' },
  offline: { text: 'Offline', tone: 'off',
    explain: 'Its agent host is offline, or no longer reports this agent. It comes back by itself when that machine and its DAASH runner run again.' },
  disabled: { text: 'Disabled', tone: 'off',
    explain: 'You turned it off. Delivery rows that use it get DAASH’s built-in agent meanwhile.' },
}
export const connectedStatus = (a) =>
  CONNECTED_STATUS[a && a.availability] || { text: String((a && a.availability) || 'unknown'), tone: 'off', explain: '' }

// "Jane · A2A", "Fitz · agent host": the roster badge of a connected agent.
export const connectedBadge = (a) => (a ? `${a.name} · ${a.protocol_label || a.protocol}` : 'connected agent')

// "fake-jane:18789" from "http://fake-jane:18789/...".
export function hostPort(url) {
  if (!url) return ''
  try { const u = new URL(url); return u.port ? `${u.hostname}:${u.port}` : u.hostname } catch { return String(url) }
}

// Where a connected agent runs, in words.
export function connectedWhere(a) {
  if (a.protocol === 'runner') return a.runner ? `on agent host ${a.runner.name}` : 'on an agent host'
  return a.url ? `at ${hostPort(a.url)}` : ''
}

// "in 42m 3s" / "3m 10s ago" for an ISO time.
export function relTime(iso) {
  if (!iso) return ''
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return String(iso)
  const sec = (t - Date.now()) / 1000
  return sec >= 0 ? `in ${humanDuration(Math.max(sec, 1))}` : `${humanDuration(-sec)} ago`
}

// Copy text to the clipboard; falls back to a hidden textarea where the async API is missing
// (DAASHboard opened over plain http from another machine). Resolves true when it worked.
export async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch { /* fall back below */ }
  try {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.setAttribute('readonly', '')
    ta.style.position = 'fixed'
    ta.style.top = '-1000px'
    document.body.appendChild(ta)
    ta.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(ta)
    return ok
  } catch { return false }
}

// The DAASH tool a `tool` event names ("Delivery-1 (Jane) used DAASH tool write_file (...)").
export function toolOfEvent(ev) {
  const m = /used DAASH tool ([A-Za-z_]+)/.exec((ev && ev.detail) || '')
  return m ? m[1] : 'tool'
}
