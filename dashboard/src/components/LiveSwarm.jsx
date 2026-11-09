import React, { useMemo, useState } from 'react'
import { api } from '../api.js'
import { usePolling } from '../hooks/usePolling.js'
import { actorNames } from '../util.js'
import { Empty, ErrorNote, Spinner } from './Common.jsx'
import SwarmHeader from './SwarmHeader.jsx'
import MetricsStrip from './MetricsStrip.jsx'
import TeamCard from './TeamCard.jsx'
import TaskDrawer from './TaskDrawer.jsx'
import RemoteJobs from './RemoteJobs.jsx'
import Modal from './Modal.jsx'
import AgentCalls from './AgentCalls.jsx'
import Artifacts from './Artifacts.jsx'
import { ActivityFeed, DecisionBus } from './Feeds.jsx'
import { Learnings, Proposals } from './Lists.jsx'

const TABS = ['Activity', 'Decision Bus', 'Artifacts', 'Learnings', 'Proposals']

export default function LiveSwarm({ swarm, onSwarmChanged }) {
  const id = swarm.id
  const board = usePolling(() => api.getBoard(id), [id])
  const metrics = usePolling(() => api.getMetrics(id), [id])
  const events = usePolling(() => api.getEvents(id, 100), [id])
  const [taskId, setTaskId] = useState(null)
  const [agent, setAgent] = useState(null)
  const [tab, setTab] = useState('Activity')
  const [filePath, setFilePath] = useState(null)

  const b = board.data
  const teams = (b && b.teams) || []
  const names = useMemo(() => actorNames(b), [b])
  const teamsById = useMemo(() => Object.fromEntries(teams.map((e) => [e.team.id, e.team])), [teams])
  const tasksById = useMemo(
    () => Object.fromEntries(teams.flatMap((e) => (e.tasks || []).map((t) => [t.id, t]))), [teams])
  const teamNames = useMemo(() => Object.fromEntries(teams.map((e) => [e.team.id, e.team.name])), [teams])

  const refreshAll = () => { board.refresh(); metrics.refresh(); events.refresh(); onSwarmChanged() }
  const live = (b && b.swarm) || swarm
  const openFile = (path) => { setTaskId(null); setFilePath(path); setTab('Artifacts') }
  const firstError = board.error || metrics.error || events.error

  return (
    <div className="live">
      <SwarmHeader swarm={live} metrics={metrics.data} onChanged={refreshAll} />
      {firstError && (
        <ErrorNote error={new Error(
          firstError.status === 404 ? 'Swarm data not found (it may be being deleted).'
            : firstError.status === 0 ? 'Cannot reach the coordinator; showing last known data.'
              : firstError.message)} />
      )}
      <MetricsStrip metrics={metrics.data} />

      {b && b.coordinator && (
        <div className="coord muted">
          Swarm Coordinator <button className="link" onClick={() => setAgent(b.coordinator)}>{b.coordinator.name}</button>
          {' '}({b.coordinator.model}) is {b.coordinator.state}
        </div>
      )}

      {board.loading && !b ? <Spinner label="Loading board" /> : !teams.length ? (
        <Empty>Swarm Coordinator is forming teams…</Empty>
      ) : (
        <div className="teams">
          {teams.map((e) => (
            <TeamCard key={e.team.id} entry={e} teamsById={teamsById}
              onOpenTask={setTaskId} onAgent={setAgent} onChanged={refreshAll} />
          ))}
        </div>
      )}

      <RemoteJobs swarmId={id} tasksById={tasksById} />

      <section className="card panel">
        <div className="tabs" role="tablist">
          {TABS.map((t) => (
            <button key={t} role="tab" aria-selected={tab === t} className={`tab ${tab === t ? 'on' : ''}`} onClick={() => setTab(t)}>{t}</button>
          ))}
        </div>
        {tab === 'Activity' && <ActivityFeed events={events.data && events.data.events} names={names} teamNames={teamNames} onOpenTask={setTaskId} />}
        {tab === 'Decision Bus' && <DecisionBus teamNames={teamNames} />}
        {tab === 'Artifacts' && <Artifacts swarmId={id} requestedPath={filePath} />}
        {tab === 'Learnings' && <Learnings swarmId={id} teamNames={teamNames} />}
        {tab === 'Proposals' && <Proposals swarmId={id} teamNames={teamNames} names={names} />}
      </section>

      {taskId && (
        <TaskDrawer taskId={taskId} names={names} onClose={() => setTaskId(null)}
          onChanged={refreshAll} onOpenTask={setTaskId} onOpenFile={openFile} />
      )}
      {agent && (
        <Modal title={`${agent.name} - recent calls`} onClose={() => setAgent(null)} wide>
          <AgentCalls agent={agent} />
        </Modal>
      )}
    </div>
  )
}
