import React, { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { ErrorNote, Spinner } from './Common.jsx'
import '../help.css'

// DAASHboard Help page (#/help). The coordinator renders the help from this version of DAASH's
// own prompt code (GET /help/prompts, app/help.py), so it always matches what the agents are
// really sent. Links inside the help become #/help/<section>, so a section can be linked to and
// the browser's Back button works.
export default function Help({ section }) {
  const [page, setPage] = useState(null)
  const [error, setError] = useState(null)
  const doc = useRef(null)

  const load = useCallback(() => {
    setError(null)
    api.getHelp('prompts').then(setPage, setError)
  }, [])
  useEffect(() => { load() }, [load])

  const show = useCallback((id) => {
    const el = id && doc.current && doc.current.querySelector(`#${CSS.escape(id)}`)
    if (el) el.scrollIntoView()
    else window.scrollTo(0, 0)
  }, [])
  useEffect(() => { if (page) show(section) }, [page, section, show])

  const onClick = (e) => {
    const a = e.target.closest('a[href^="#"]')
    if (!a || !doc.current.contains(a)) return
    e.preventDefault()
    const id = a.getAttribute('href').slice(1)
    if (id === section) show(id)
    else window.location.hash = `#/help/${encodeURIComponent(id)}`
  }

  return (
    <main className="container">
      {error && (
        <div className="help-doc">
          <ErrorNote error={new Error(`Cannot load the help: ${error.message}`)} />
          <button type="button" className="btn small" onClick={load}>Try again</button>
        </div>
      )}
      {!page && !error && <Spinner label="Loading help" />}
      {page && (
        // The HTML comes from our own coordinator, built from DAASH's code with escaped sample data.
        <article ref={doc} className="help-doc" onClick={onClick} dangerouslySetInnerHTML={{ __html: page.html }} />
      )}
    </main>
  )
}
