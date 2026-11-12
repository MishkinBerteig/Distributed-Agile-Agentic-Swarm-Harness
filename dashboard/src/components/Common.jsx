import React, { useEffect, useRef, useState } from 'react'
import { copyText, statusClass } from '../util.js'

// A Copy button for `text`; says "Copied" for a moment, or how to copy by hand if it could not.
export function CopyButton({ text, label = 'Copy', onFail }) {
  const [state, setState] = useState('')
  const timer = useRef(null)
  useEffect(() => () => clearTimeout(timer.current), [])
  const copy = async () => {
    const ok = await copyText(text)
    setState(ok ? 'ok' : 'fail')
    if (!ok && onFail) onFail()
    clearTimeout(timer.current)
    timer.current = setTimeout(() => setState(''), 2500)
  }
  return (
    <button type="button" className="btn small copy-btn" onClick={copy} aria-live="polite">
      {state === 'ok' ? 'Copied ✓' : state === 'fail' ? 'Select it and copy' : label}
    </button>
  )
}

// A block of text to paste somewhere else (a command, a message), with its Copy button.
export function CopyBlock({ title, text, copyLabel = 'Copy', children }) {
  const pre = useRef(null)
  const selectAll = () => {
    const sel = window.getSelection && window.getSelection()
    if (!sel || !pre.current) return
    const range = document.createRange()
    range.selectNodeContents(pre.current)
    sel.removeAllRanges()
    sel.addRange(range)
  }
  return (
    <div className="copy-block">
      <div className="copy-head">
        {title && <span className="copy-title">{title}</span>}
        <CopyButton text={text} label={copyLabel} onFail={selectAll} />
      </div>
      {children}
      <pre ref={pre} className="cmd" tabIndex={0}>{text}</pre>
    </div>
  )
}

export const Spinner = ({ label }) => (
  <span className="spinner-wrap"><span className="spinner" aria-hidden="true" />{label && <span> {label}</span>}</span>
)

export const ErrorNote = ({ error }) =>
  error ? <div className="error-note" role="alert">{error.message || String(error)}</div> : null

export const Empty = ({ children }) => <div className="empty">{children}</div>

export const Chip = ({ status, children, title }) => (
  <span className={`chip ${statusClass(status)}`} title={title}>{children ?? status}</span>
)

export function Collapsible({ title, defaultOpen = false, right, children }) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <section className="collapsible">
      <button type="button" className="collapsible-head" onClick={() => setOpen(!open)} aria-expanded={open}>
        <span className="caret">{open ? '▾' : '▸'}</span>
        <span className="collapsible-title">{title}</span>
        {right && <span className="collapsible-right">{right}</span>}
      </button>
      {open && <div className="collapsible-body">{children}</div>}
    </section>
  )
}

export function Bar({ label, value, max, text }) {
  const pct = max ? Math.min(100, (100 * value) / max) : 0
  const cls = pct >= 90 ? 'bar-hot' : pct >= 70 ? 'bar-warn' : ''
  return (
    <div className="bar-row">
      <div className="bar-label"><span>{label}</span><span className="muted">{text}</span></div>
      <div className="bar-track"><div className={`bar-fill ${cls}`} style={{ width: `${pct}%` }} /></div>
    </div>
  )
}

// Runs an async action, tracking busy + error state.
export function useAction() {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const run = async (fn) => {
    setBusy(true); setError(null)
    try { return await fn() } catch (e) { setError(e); return undefined } finally { setBusy(false) }
  }
  return { busy, error, run, clearError: () => setError(null) }
}
