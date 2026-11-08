import { useCallback, useEffect, useRef, useState } from 'react'

// Polls `fn` every `interval` ms. Pauses while the tab is hidden and refreshes on
// becoming visible. Keeps the last good data when a poll fails and exposes `error`.
export function usePolling(fn, deps = [], { interval = 3000, enabled = true } = {}) {
  const [state, setState] = useState({ data: undefined, error: null, loading: true })
  const fnRef = useRef(fn)
  fnRef.current = fn
  const tick = useRef(0)

  const run = useCallback(async () => {
    const id = ++tick.current
    try {
      const data = await fnRef.current()
      if (id === tick.current) setState({ data, error: null, loading: false })
    } catch (error) {
      if (id === tick.current) setState((s) => ({ ...s, error, loading: false }))
    }
  }, [])

  useEffect(() => {
    if (!enabled) return undefined
    setState({ data: undefined, error: null, loading: true })
    let timer = null
    let stopped = false
    const schedule = () => {
      timer = setTimeout(async () => {
        if (!document.hidden) await run()
        if (!stopped) schedule()
      }, interval)
    }
    run().then(() => { if (!stopped) schedule() })
    const onVis = () => { if (!document.hidden) run() }
    document.addEventListener('visibilitychange', onVis)
    return () => {
      stopped = true
      tick.current++
      clearTimeout(timer)
      document.removeEventListener('visibilitychange', onVis)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, interval, run, ...deps])

  return { ...state, refresh: run }
}

// One-shot loader (no polling) with the same shape.
export function useLoad(fn, deps = []) {
  const [state, setState] = useState({ data: undefined, error: null, loading: true })
  const fnRef = useRef(fn)
  fnRef.current = fn
  const load = useCallback(async () => {
    try { setState({ data: await fnRef.current(), error: null, loading: false }) }
    catch (error) { setState((s) => ({ ...s, error, loading: false })) }
  }, [])
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { setState({ data: undefined, error: null, loading: true }); load() }, [load, ...deps])
  return { ...state, refresh: load }
}
