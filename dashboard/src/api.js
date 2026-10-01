const BASE = (import.meta.env.VITE_API_URL || '/api').replace(/\/$/, '')

async function request(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail ?? body)
    } catch { /* keep statusText */ }
    throw new Error(detail)
  }
  return res.status === 204 ? null : res.json()
}

export const api = {
  listTeams: () => request('/teams').then((d) => d.teams),
  createSwarm: (payload) => request('/swarms', { method: 'POST', body: JSON.stringify(payload) }),
  listTasks: (teamId, status) => {
    const params = new URLSearchParams({ team_id: teamId })
    if (status) params.set('status', status)
    return request(`/tasks?${params}`).then((d) => d.tasks)
  },
  createTask: (payload) => request('/tasks', { method: 'POST', body: JSON.stringify(payload) }),

  // --- Decision Bus (Slice 2) ---
  listDecisions: () => request('/decisions').then((d) => d.signals),
  publishDecision: (payload) => request('/decisions', { method: 'POST', body: JSON.stringify(payload) }),
  listProposals: (teamId) => {
    const params = teamId ? `?team_id=${encodeURIComponent(teamId)}` : ''
    return request(`/proposals${params}`).then((d) => d.proposals)
  },
  createProposal: (payload) => request('/proposals', { method: 'POST', body: JSON.stringify(payload) }),
  castVote: (proposalId, payload) => request(`/proposals/${encodeURIComponent(proposalId)}/votes`, { method: 'POST', body: JSON.stringify(payload) }),
  getProposal: (proposalId) => request(`/proposals/${encodeURIComponent(proposalId)}`),
}
