import React from 'react'
import { ACTION_LABEL, SWARM_ACTIONS } from '../util.js'

// The swarm lifecycle as a graph, with the current state highlighted and the moves open from it
// drawn in the accent colour. The edges come from SWARM_ACTIONS (mirroring app/models.py), so
// the picture changes when the lifecycle does; only the layout is fixed here.
const COL = 172
const ROW = 84
const NODE_W = 112
const NODE_H = 30
const WIDTH = 2 * 66 + 4 * COL
const HEIGHT = 2 * 22 + 2 * ROW
const at = (col, row) => ({ x: 66 + col * COL, y: 22 + (row + 1) * ROW })

const NODES = {
  DELETED: at(3, -1),
  CREATED: at(0, 0),
  ACTIVE: at(1, 0),
  VERIFICATION: at(3, 0),
  LEARNING: at(4, 0),
  USER_FEEDBACK: at(0, 1),
  PAUSED: at(1, 1),
  ARCHIVED: at(4, 1),
}
const NODE_LABEL = { USER_FEEDBACK: 'USER FEEDBACK' }
const TERMINAL = new Set(['ARCHIVED', 'DELETED'])

// Diagram names for moves that are not buttons.
const EDGE_LABEL = { feedback: 'Send feedback' }
const edgeLabel = (a) => EDGE_LABEL[a] || ACTION_LABEL[a] || a

// Moves the engine also makes by itself.
const AUTO = {
  'ACTIVE>PAUSED': 'or a limit is hit',
  'ACTIVE>VERIFICATION': 'or all teams finish',
}

export const STATE_TEXT = {
  CREATED: 'Set up but not started. Start it, or delete it.',
  ACTIVE: 'Running: agents are working. Pause stops new work; Verify stops it for your review. '
    + 'The swarm also pauses by itself when a limit is hit, and moves to verification by itself '
    + 'when every team has finished.',
  PAUSED: 'Paused: no new work starts. Resume, send the agents feedback, or Verify to stop and review.',
  USER_FEEDBACK: 'Your feedback is recorded for every agent in the swarm. Resume to carry on with it.',
  VERIFICATION: 'Work has stopped. Review the artifacts, then Learn (a retrospective that writes '
    + 'learnings for future swarms), Archive, or Delete.',
  LEARNING: 'The Swarm Coordinator is writing the retrospective. Archive the swarm when it is done.',
  ARCHIVED: 'Finished and kept. Its learnings help future swarms.',
  DELETED: 'Deleted.',
}

const EDGES = Object.entries(SWARM_ACTIONS).flatMap(([action, moves]) =>
  Object.entries(moves).map(([from, to]) => ({ action, from, to })))
  .filter((e) => NODES[e.from] && NODES[e.to])   // a state with no place in the layout is left out
const paired = (e) => EDGES.some((o) => o.from === e.to && o.to === e.from)

// Where the segment from centre a towards b leaves a's box (with a small gap).
function exit(a, b) {
  const dx = b.x - a.x
  const dy = b.y - a.y
  const t = Math.min(dx ? (NODE_W / 2 + 4) / Math.abs(dx) : Infinity, dy ? (NODE_H / 2 + 4) / Math.abs(dy) : Infinity)
  return { x: a.x + dx * t, y: a.y + dy * t }
}

function geometry(e) {
  const a = NODES[e.from]
  const b = NODES[e.to]
  if (e.from === 'CREATED' && e.to === 'DELETED') {   // routed over the top
    const y = b.y
    return { d: `M ${a.x} ${a.y - NODE_H / 2 - 4} L ${a.x} ${y} L ${b.x - NODE_W / 2 - 4} ${y}`,
      label: { x: (a.x + b.x) / 2 - 40, y, anchor: 'middle' } }
  }
  const len = Math.hypot(b.x - a.x, b.y - a.y)
  // The right-hand side when walking from a to b: each arrow of a two-way pair, and its label, sits on its own side.
  const nx = (b.y - a.y) / len
  const ny = -(b.x - a.x) / len
  const pair = paired(e)
  const off = pair ? 6 : 0
  const s = exit({ x: a.x + nx * off, y: a.y + ny * off }, { x: b.x + nx * off, y: b.y + ny * off })
  const t = exit({ x: b.x + nx * off, y: b.y + ny * off }, { x: a.x + nx * off, y: a.y + ny * off })
  const lo = pair ? 12 : 0
  const anchor = pair && Math.abs(nx) > 0.7 ? (nx > 0 ? 'start' : 'end') : 'middle'
  // A one-way horizontal arrow carries its label above the line (and any note below it).
  const above = !pair && a.y === b.y
  return { d: `M ${s.x} ${s.y} L ${t.x} ${t.y}`, above,
    label: { x: (s.x + t.x) / 2 + nx * lo, y: (s.y + t.y) / 2 + ny * lo - (above ? 8 : 0), anchor } }
}

export default function StateDiagram({ state }) {
  const open = EDGES.filter((e) => e.from === state)
  return (
    <figure className="lifecycle">
      <div className="lifecycle-scroll">
        <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img" className="lifecycle-svg"
          aria-label={`Swarm lifecycle. Current state ${state}. Possible next: ${open.map((e) => `${edgeLabel(e.action)} to ${e.to}`).join(', ') || 'none'}.`}>
          <defs>
            <marker id="lc-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M 0 0 L 10 5 L 0 10 z" className="lc-head" />
            </marker>
            <marker id="lc-arrow-on" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M 0 0 L 10 5 L 0 10 z" className="lc-head on" />
            </marker>
          </defs>
          {EDGES.map((e) => {
            const g = geometry(e)
            const on = e.from === state
            const auto = AUTO[`${e.from}>${e.to}`]
            return (
              <g key={`${e.from}>${e.to}`} className={`lc-edge${on ? ' on' : ''}`}>
                <path d={g.d} markerEnd={`url(#${on ? 'lc-arrow-on' : 'lc-arrow'})`} />
                <text x={g.label.x} y={g.label.y} textAnchor={g.label.anchor} dominantBaseline="middle">
                  {edgeLabel(e.action)}
                  {auto && <tspan x={g.label.x} dy={g.above ? 17 : 11} className="lc-auto">{auto}</tspan>}
                </text>
              </g>
            )
          })}
          {Object.entries(NODES).map(([name, p]) => (
            <g key={name} className={`lc-node${name === state ? ' current' : ''}${TERMINAL.has(name) ? ' terminal' : ''}`}>
              <title>{`${name}: ${STATE_TEXT[name] || ''}`}</title>
              <rect x={p.x - NODE_W / 2} y={p.y - NODE_H / 2} width={NODE_W} height={NODE_H} rx="16" />
              <text x={p.x} y={p.y} textAnchor="middle" dominantBaseline="central">{NODE_LABEL[name] || name}</text>
            </g>
          ))}
        </svg>
      </div>
      <figcaption><strong>{NODE_LABEL[state] || state}.</strong> {STATE_TEXT[state] || ''}</figcaption>
    </figure>
  )
}
