import React, { useEffect, useRef, useState } from 'react'
import { PAGES } from '../router.js'
import { Chip } from './Common.jsx'

// The top bar: the DAASHboard menu (a row of links on wide screens, a Menu button on narrow
// ones) and Sign out. The Swarm link carries the live swarm's state.
export default function TopBar({ route, swarm, nav = true, showSignOut, onSignOut }) {
  const [open, setOpen] = useState(false)
  const bar = useRef(null)

  useEffect(() => { setOpen(false) }, [route.page, route.sub])
  useEffect(() => {
    if (!open) return undefined
    const onKey = (e) => { if (e.key === 'Escape') setOpen(false) }
    const onClick = (e) => { if (bar.current && !bar.current.contains(e.target)) setOpen(false) }
    document.addEventListener('keydown', onKey)
    document.addEventListener('click', onClick)
    return () => { document.removeEventListener('keydown', onKey); document.removeEventListener('click', onClick) }
  }, [open])

  return (
    <header className="topbar" ref={bar}>
      <a className="brand" href="#/" title="Distributed Agile Agentic Swarm Harness">DAASHboard</a>
      <span className="muted tagline">Distributed Agile Agentic Swarm Harness</span>
      {nav && (
        <nav className="mainnav" aria-label="DAASHboard">
          <button type="button" className="btn small menu-toggle" aria-expanded={open}
            aria-controls="mainnav-links" onClick={() => setOpen(!open)}>
            <span aria-hidden="true">☰</span> Menu
          </button>
          <ul id="mainnav-links" className={`mainnav-links${open ? ' open' : ''}`}>
            {PAGES.map((p) => (
              <li key={p.id}>
                <a href={p.hash} aria-current={route.page === p.id ? 'page' : undefined}>
                  {p.label}
                  {p.id === 'swarm' && swarm && <Chip status={swarm.lifecycle_state} title={`${swarm.name} is ${swarm.lifecycle_state}`} />}
                </a>
              </li>
            ))}
          </ul>
        </nav>
      )}
      {showSignOut && <button type="button" className="btn small signout" onClick={onSignOut}>Sign out</button>}
    </header>
  )
}
