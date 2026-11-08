import React, { useState } from 'react'
import { api } from '../api.js'
import { CHECK_KINDS } from '../util.js'
import { Collapsible, ErrorNote, Spinner, useAction } from './Common.jsx'

const blank = () => ({ text: '', check_kind: 'rubric', check_path: '', check_arg: '' })

export default function AddTask({ team, onAdded }) {
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [artifact, setArtifact] = useState('')
  const [crit, setCrit] = useState([blank()])
  const { busy, error, run } = useAction()
  const setC = (i, p) => setCrit(crit.map((c, j) => (j === i ? { ...c, ...p } : c)))

  const submit = (e) => {
    e.preventDefault()
    const acceptance_criteria = crit.filter((c) => c.text.trim()).map((c) => ({
      text: c.text.trim(),
      check_kind: c.check_kind,
      check_path: c.check_path.trim() || null,
      check_arg: c.check_arg.trim() || null,
    }))
    run(async () => {
      await api.createTask({ team_id: team.id, name: name.trim(), description, artifact: artifact.trim(), acceptance_criteria })
      setName(''); setDescription(''); setArtifact(''); setCrit([blank()])
      onAdded()
    })
  }

  return (
    <Collapsible title="Add task">
      <form className="form" onSubmit={submit}>
        <label>Name<input required value={name} onChange={(e) => setName(e.target.value)} /></label>
        <label>Description<textarea rows={2} value={description} onChange={(e) => setDescription(e.target.value)} /></label>
        <label>Artifact path<input value={artifact} onChange={(e) => setArtifact(e.target.value)} placeholder="e.g. docs/intro.md" /></label>
        <fieldset>
          <legend>Acceptance criteria</legend>
          {crit.map((c, i) => (
            <div className="crit-row" key={i}>
              <input className="grow" placeholder="Criterion" value={c.text} onChange={(e) => setC(i, { text: e.target.value })} />
              <select value={c.check_kind} onChange={(e) => setC(i, { check_kind: e.target.value })}>
                {CHECK_KINDS.map((k) => <option key={k}>{k}</option>)}
              </select>
              <input placeholder="path" value={c.check_path} onChange={(e) => setC(i, { check_path: e.target.value })} />
              <input placeholder="arg" value={c.check_arg} onChange={(e) => setC(i, { check_arg: e.target.value })} />
              <button type="button" className="btn small" onClick={() => setCrit(crit.filter((_, j) => j !== i))}>x</button>
            </div>
          ))}
          <button type="button" className="btn small" onClick={() => setCrit([...crit, blank()])}>+ criterion</button>
        </fieldset>
        <ErrorNote error={error} />
        <div><button className="btn primary" disabled={busy || !name.trim()}>{busy ? <Spinner label="Adding" /> : 'Add task'}</button></div>
      </form>
    </Collapsible>
  )
}
