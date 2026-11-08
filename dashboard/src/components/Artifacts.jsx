import React, { useEffect, useMemo, useState } from 'react'
import { api } from '../api.js'
import { usePolling, useLoad } from '../hooks/usePolling.js'
import { buildArtifactTree, fmtNum } from '../util.js'
import { Empty, ErrorNote, Spinner } from './Common.jsx'

function Tree({ node, selected, onSelect, depth = 0 }) {
  return (
    <ul className="tree" style={{ paddingLeft: depth ? 14 : 0 }}>
      {Object.entries(node.children).sort().map(([name, child]) => (
        <li key={name}><span className="tree-dir">{name}/</span><Tree node={child} selected={selected} onSelect={onSelect} depth={depth + 1} /></li>
      ))}
      {node.files.map((f) => (
        <li key={f.path}>
          <button type="button" className={`link ${selected === f.path ? 'sel' : ''}`} onClick={() => onSelect(f.path)}>{f.name}</button>
          <span className="muted"> {fmtNum(f.size)} B</span>
        </li>
      ))}
    </ul>
  )
}

function Viewer({ swarmId, path }) {
  const { data, error, loading } = useLoad(() => api.getArtifact(swarmId, path), [swarmId, path])
  if (loading) return <Spinner label="Loading file" />
  if (error) return <ErrorNote error={error} />
  return (
    <div>
      <div className="muted">{data.path}{data.truncated ? ' (truncated)' : ''}</div>
      <pre className="viewer">{data.content}</pre>
    </div>
  )
}

export default function Artifacts({ swarmId, requestedPath }) {
  const { data, error, loading } = usePolling(() => api.listArtifacts(swarmId), [swarmId])
  const [selected, setSelected] = useState(null)
  useEffect(() => { if (requestedPath) setSelected(requestedPath) }, [requestedPath])
  const tree = useMemo(() => buildArtifactTree(data && data.files), [data])
  const files = (data && data.files) || []

  if (loading) return <Spinner label="Loading artifacts" />
  return (
    <div className="artifacts">
      <div className="art-tree">
        <ErrorNote error={error} />
        {!files.length ? <Empty>The workspace has no files yet.</Empty> : <Tree node={tree} selected={selected} onSelect={setSelected} />}
      </div>
      <div className="art-view">
        {selected ? <Viewer swarmId={swarmId} path={selected} /> : <Empty>Select a file to view it.</Empty>}
      </div>
    </div>
  )
}
