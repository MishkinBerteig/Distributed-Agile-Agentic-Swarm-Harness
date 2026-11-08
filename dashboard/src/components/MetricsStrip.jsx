import React from 'react'
import { fmtNum, humanDuration, statusClass, TASK_COLUMNS, TERMINAL_COLUMNS } from '../util.js'

function Sparkline({ series }) {
  const W = 180, H = 44, P = 3
  if (!series || !series.length) return <div className="muted">no data</div>
  const max = Math.max(0.01, ...series.map((p) => p.rate || 0))
  const step = series.length > 1 ? (W - 2 * P) / (series.length - 1) : 0
  const pts = series.map((p, i) => [P + i * step, H - P - ((p.rate || 0) / max) * (H - 2 * P)])
  const line = pts.map((p) => p.join(',')).join(' ')
  const last = series[series.length - 1]
  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} role="img"
      aria-label={`Error rate, latest ${(100 * (last.rate || 0)).toFixed(0)} percent`}>
      <line x1={P} x2={W - P} y1={H - P} y2={H - P} className="spark-axis" />
      <polyline points={line} className="spark-line" fill="none" />
      {pts.map((p, i) => <circle key={i} cx={p[0]} cy={p[1]} r="1.8" className="spark-dot"><title>
        {`${series[i].errors}/${series[i].calls} errors`}</title></circle>)}
    </svg>
  )
}

export default function MetricsStrip({ metrics }) {
  if (!metrics) return null
  const byStatus = metrics.tasks_by_status || {}
  const series = (metrics.error_rate && metrics.error_rate.series) || []
  const last = series[series.length - 1]
  const tokens = metrics.tokens || {}
  const byModel = Object.entries(tokens.by_model || {}).sort((a, b) => b[1] - a[1])
  const maxModel = Math.max(1, ...byModel.map(([, v]) => v))
  const failRates = metrics.team_failure_rates || []

  return (
    <section className="metrics">
      <div className="card metric">
        <div className="metric-label">p90 cycle time</div>
        <div className="metric-big">{humanDuration(metrics.p90_cycle_seconds)}</div>
        <div className="muted">committed to done</div>
      </div>
      <div className="card metric">
        <div className="metric-label">Tasks by status</div>
        <div className="status-counts">
          {[...TASK_COLUMNS, ...TERMINAL_COLUMNS].map((s) => (
            <span key={s} className={`chip ${statusClass(s)}`}>{s} <b>{byStatus[s] || 0}</b></span>
          ))}
        </div>
      </div>
      <div className="card metric">
        <div className="metric-label">Error rate{last ? ` (now ${(100 * (last.rate || 0)).toFixed(0)}%)` : ''}</div>
        <Sparkline series={series} />
      </div>
      <div className="card metric">
        <div className="metric-label">Team failure rate</div>
        {failRates.length ? (
          <table className="table compact">
            <thead><tr><th>Team</th><th>Committed</th><th>Fail/task</th></tr></thead>
            <tbody>{failRates.map((t) => (
              <tr key={t.team_id}><td>{t.team_name}</td><td>{t.tasks_committed}</td>
                <td>{(t.failures_per_task ?? 0).toFixed(2)}</td></tr>
            ))}</tbody>
          </table>
        ) : <div className="muted">no data</div>}
      </div>
      <div className="card metric">
        <div className="metric-label">Tokens by model <span className="muted">({fmtNum(tokens.total || 0)} total)</span></div>
        {byModel.length ? byModel.map(([m, v]) => (
          <div className="mini-bar" key={m}>
            <span className="mini-name">{m}</span>
            <span className="mini-track"><span style={{ width: `${(100 * v) / maxModel}%` }} /></span>
            <span className="muted">{fmtNum(v)}</span>
          </div>
        )) : <div className="muted">no calls yet</div>}
      </div>
    </section>
  )
}
