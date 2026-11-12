import { useEffect, useState } from 'react'

// DAASHboard pages, addressed by the URL hash so links, Back and Forward work:
// #/ (the swarm), #/swarms, #/agents, #/settings, #/help, #/help/<section>.
export const PAGES = [
  { id: 'swarm', label: 'Swarm', hash: '#/' },
  { id: 'swarms', label: 'Past swarms', hash: '#/swarms' },
  { id: 'agents', label: 'Agents & models', hash: '#/agents' },
  { id: 'settings', label: 'Settings', hash: '#/settings' },
  { id: 'help', label: 'Help', hash: '#/help' },
]

export function parseHash(hash) {
  const [first, ...rest] = String(hash || '').replace(/^#\/?/, '').split('/')
  const page = PAGES.find((p) => p.id === first)
  if (!page) return { page: 'swarm', sub: '' }
  let sub = rest.join('/')
  try { sub = decodeURIComponent(sub) } catch { /* keep it as typed */ }
  return { page: page.id, sub }
}

export function useRoute() {
  const [route, setRoute] = useState(() => parseHash(window.location.hash))
  useEffect(() => {
    const onHash = () => setRoute(parseHash(window.location.hash))
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])
  return route
}
