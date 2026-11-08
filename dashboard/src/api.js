// Every coordinator endpoint the DAASHboard uses (DESIGN-0.1 section 9).
// Errors throw Error(server `detail`) with `.status` set (0 = unreachable).
const BASE = '/api'

async function request(method, path, body) {
  let res
  try {
    res = await fetch(BASE + path, {
      method,
      headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    })
  } catch {
    const err = new Error('Coordinator unreachable')
    err.status = 0
    throw err
  }
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`
    try {
      const j = await res.json()
      if (j && j.detail !== undefined) {
        detail = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail)
      }
    } catch { /* non-JSON body */ }
    const err = new Error(detail)
    err.status = res.status
    throw err
  }
  if (res.status === 204) return null
  return res.json()
}

const q = (params) => {
  const s = Object.entries(params)
    .filter(([, v]) => v !== undefined && v !== null && v !== '')
    .map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join('&')
  return s ? `?${s}` : ''
}
const enc = encodeURIComponent

export const api = {
  health: () => request('GET', '/health'),
  getConfig: () => request('GET', '/config'),
  putConfig: (cfg) => request('PUT', '/config', cfg),
  getModels: () => request('GET', '/models'),

  listSwarms: () => request('GET', '/swarms'),
  createSwarm: (payload) => request('POST', '/swarms', payload),
  getActiveSwarm: () => request('GET', '/swarms/active'),
  transition: (id, action) => request('POST', `/swarms/${id}/transitions/${action}`),
  postFeedback: (id, text) => request('POST', `/swarms/${id}/feedback`, { text }),
  deleteSwarm: (id) => request('DELETE', `/swarms/${id}`),
  getBoard: (id) => request('GET', `/swarms/${id}/board`),
  getMetrics: (id) => request('GET', `/swarms/${id}/metrics`),
  getEvents: (id, limit = 100) => request('GET', `/swarms/${id}/events${q({ limit })}`),
  listArtifacts: (id) => request('GET', `/swarms/${id}/artifacts`),
  getArtifact: (id, path) =>
    request('GET', `/swarms/${id}/artifacts/${path.split('/').map(enc).join('/')}`),
  getLearnings: (id) => request('GET', `/swarms/${id}/learnings`),
  getProposals: (id) => request('GET', `/swarms/${id}/proposals`),

  getAgentCalls: (id, limit = 20) => request('GET', `/agents/${id}/calls${q({ limit })}`),

  generateQuality: (teamId, dor_guidance, dod_guidance) =>
    request('POST', `/teams/${teamId}/quality`, { dor_guidance, dod_guidance }),

  getTask: (id) => request('GET', `/tasks/${id}`),
  createTask: (payload) => request('POST', '/tasks', payload),
  updateTask: (id, patch) => request('PATCH', `/tasks/${id}`, patch),
  deleteTask: (id) => request('DELETE', `/tasks/${id}`),

  getDecisions: (limit = 50) => request('GET', `/decisions${q({ limit })}`),
}
