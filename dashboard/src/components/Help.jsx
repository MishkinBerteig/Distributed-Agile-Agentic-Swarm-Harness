import React, { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { ErrorNote, Spinner } from './Common.jsx'
import '../help.css'

// DAASHboard Help page. The coordinator renders each topic from this version of DAASH's own code
// (GET /help lists the topics, GET /help/<topic> returns {title, html}), so the help always
// matches what DAASH really does. Routes:
//   #/help, #/help/<section>                 "How DAASH prompts its agents" (the first topic)
//   #/help/<topic>, #/help/<topic>/<section> any other topic, e.g. #/help/connect/approval
// Links inside a topic's HTML (href="#<id>") become those routes, so a section can be linked to
// and the browser's Back button works.
const DEFAULT_TOPIC = 'prompts'
const KNOWN_TOPICS = [
  { id: 'prompts', title: 'How DAASH prompts its agents' },
  { id: 'connect', title: 'Connecting agents' },
]

export function helpRoute(sub, topics = KNOWN_TOPICS) {
  const [first, ...rest] = String(sub || '').split('/')
  if (first && topics.some((t) => t.id === first)) return { topic: first, section: rest.join('/') }
  return { topic: DEFAULT_TOPIC, section: String(sub || '') }
}

export const helpHref = (topic, section) => {
  const s = section ? `/${encodeURIComponent(section)}` : ''
  return topic === DEFAULT_TOPIC ? `#/help${s}` : `#/help/${topic}${s}`
}

export default function Help({ section: sub }) {
  const [topics, setTopics] = useState(KNOWN_TOPICS)
  const { topic, section } = helpRoute(sub, topics)
  const [pages, setPages] = useState({})
  const [error, setError] = useState(null)
  const doc = useRef(null)
  const page = pages[topic]

  useEffect(() => {
    api.getHelpTopics().then((r) => {
      const list = (r && r.topics) || []
      if (list.length) setTopics(list)
    }, () => { /* keep the built-in list */ })
  }, [])

  const load = useCallback((id) => {
    setError(null)
    api.getHelp(id).then((p) => setPages((all) => ({ ...all, [id]: p })), setError)
  }, [])
  useEffect(() => { if (!pages[topic]) load(topic) }, [topic]) // eslint-disable-line react-hooks/exhaustive-deps

  const show = useCallback((id) => {
    const el = id && doc.current && doc.current.querySelector(`#${CSS.escape(id)}`)
    if (el) el.scrollIntoView()
    else window.scrollTo(0, 0)
  }, [])
  useEffect(() => { if (page) show(section) }, [page, topic, section, show])

  const onClick = (e) => {
    const a = e.target.closest('a[href^="#"]')
    if (!a || !doc.current.contains(a)) return
    const href = a.getAttribute('href')
    if (href.startsWith('#/')) return          // already a DAASHboard route
    e.preventDefault()
    const id = href.slice(1)
    if (id === section) show(id)
    else window.location.hash = helpHref(topic, id)
  }

  return (
    <main className="container">
      <nav className="help-topics" aria-label="Help topics">
        <span className="muted">Help topics:</span>
        {topics.map((t) => (
          <a key={t.id} href={helpHref(t.id)} aria-current={t.id === topic ? 'true' : undefined}>{t.title}</a>
        ))}
      </nav>
      {error && !page && (
        <div className="help-doc">
          <ErrorNote error={new Error(`Cannot load the help: ${error.message}`)} />
          <button type="button" className="btn small" onClick={() => load(topic)}>Try again</button>
        </div>
      )}
      {!page && !error && <Spinner label="Loading help" />}
      {page && (
        // The HTML comes from our own coordinator, built from DAASH's code with escaped sample data.
        <article key={topic} ref={doc} className="help-doc" onClick={onClick} dangerouslySetInnerHTML={{ __html: page.html }} />
      )}
    </main>
  )
}
