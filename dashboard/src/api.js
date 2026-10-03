export const api = {
  // Swarm / lifecycle
  createSwarm(payload) { return api._post('/api/swarms', payload) },
  getActiveSwarm()   { return api._get('/api/swarms/active') },
  transition(teamId, action) {
    return api._post(`/api/swarms/${teamId}/transitions/${action}`)
  },
  deleteSwarm(teamId) { return api._del(`/api/swarms/${teamId}/delete`) },
  listTeams()         { return api._get('/api/teams') },

  // Tasks
  listTasks(teamId, status) {
    const q = status ? `?team_id=${encodeURIComponent(teamId)}&status=${status}` : `?team_id=${encodeURIComponent(teamId)}`
    return api._get(`/api/tasks${q}`)
  },
  createTask(payload) { return api._post('/api/tasks', payload) },
  getTask(id)         { return api._get(`/api/tasks/${id}`) },
  updateTask(id, body){ return api._patch(`/api/tasks/${id}`, body) },
  deleteTask(id)      { return api._del(`/api/tasks/${id}`) },
}

api._get  = (url) => fetch(url).then(r => r.json())
api._post = (url, body) => fetch(url, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) }).then(r => r.json())
api._patch= (url, body) => fetch(url, { method: 'PATCH', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) }).then(r => r.json())
api._del  = (url) => fetch(url, { method: 'DELETE' })
