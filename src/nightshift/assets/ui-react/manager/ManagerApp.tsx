/**
 * Manager / operator UI app — the larger surface, wiring the shared component
 * kit to the manager backend (:8800) plus live SSE convergence.
 *
 * Screens shown here (the core; the legacy UI has more chrome — modals, repos,
 * playlist-info — which layer on top of these same shared pieces later):
 *
 *   Queue    — shared TaskList over /api/queue, rows via queueItemToRow; a row
 *              click opens the shared TaskDetail (edit → PATCH /api/tasks)
 *   History  — shared TaskList over /api/runs, rows via runToRow
 *   Stats    — shared ManagerStatsView over /api/stats (overall + comparison tables)
 *   Workers  — fleet list + the SAME ComparisonTable component reused from Stats
 *   Settings — shared SettingsEditor over /api/settings
 *
 * useSse() drives cache convergence so reorders / check-ins / runs update live.
 */

import { useState } from 'react'
import { AppShell, NavTab } from '../src/app/AppShell'
import { TaskList } from '../src/components/TaskList'
import { TaskDetail } from '../src/components/TaskDetail'
import { ManagerStatsView, ComparisonTable } from '../src/components/StatsPage'
import { SettingsEditor } from '../src/components/SettingsEditor'
import {
  EmptyState,
  ErrorState,
  Pill,
  Spinner,
} from '../src/components/primitives'
import { queueItemToRow, runToRow } from '../src/lib/rowAdapters'
import {
  useActiveState,
  useBlocked,
  useInfo,
  useManagerStats,
  useQueueItems,
  useRuns,
  useSettings,
  useTask,
  useTransport,
  useUpdateTask,
  useWorkers,
} from '../src/hooks/managerQueries'
import { useSse } from '../src/hooks/useSse'
import { manager } from '../src/api/endpoints'

type View = 'queue' | 'history' | 'stats' | 'workers' | 'settings'

function QueueScreen() {
  const { data, isLoading, error } = useQueueItems()
  const [openTask, setOpenTask] = useState<string | null>(null)

  if (openTask) {
    return <TaskEditor task={openTask} onBack={() => setOpenTask(null)} />
  }

  return (
    <TaskList
      title="Up next"
      rows={(data ?? []).map(queueItemToRow)}
      isLoading={isLoading}
      error={error}
      emptyMessage="No pending tasks."
      onRowClick={setOpenTask}
    />
  )
}

function TaskEditor({ task, onBack }: { task: string; onBack: () => void }) {
  const { data, isLoading, error } = useTask(task)
  const update = useUpdateTask()
  if (error) return <ErrorState error={error} />
  if (isLoading || !data) return <Spinner />
  return (
    <TaskDetail
      detail={data}
      onBack={onBack}
      saving={update.isPending}
      onSave={(patch) =>
        update.mutate({ task, body: patch }, { onSuccess: onBack })
      }
    />
  )
}

function HistoryScreen() {
  const { data, isLoading, error } = useRuns()
  return (
    <TaskList
      title="History"
      rows={(data ?? []).map(runToRow)}
      isLoading={isLoading}
      error={error}
      emptyMessage="No runs yet."
    />
  )
}

function StatsScreen() {
  const { data, isLoading, error } = useManagerStats()
  if (error) return <ErrorState error={error} />
  if (isLoading || !data) return <Spinner />
  return <ManagerStatsView stats={data} />
}

function WorkersScreen() {
  const { data: workers, isLoading, error } = useWorkers()
  const { data: blocked } = useBlocked()
  const { data: stats } = useManagerStats()

  if (error) return <ErrorState error={error} />
  if (isLoading) return <Spinner />

  return (
    <div className="min-h-0 flex-1 overflow-y-auto">
      <ul className="list-none">
        {(workers ?? []).map((w) => (
          <li
            key={w.id}
            className="flex items-center gap-3 border-b border-border px-4 py-3"
          >
            <span className="font-medium text-text">{w.id}</span>
            <Pill tone={w.status === 'busy' ? 'accent' : 'neutral'}>
              {w.status}
            </Pill>
            <span className="text-xs text-text-dim">{w.backend}</span>
            {w.current_task && (
              <span className="ml-auto text-xs text-text-dim">
                {w.current_task}
              </span>
            )}
          </li>
        ))}
      </ul>
      {(workers ?? []).length === 0 && (
        <EmptyState>No workers checked in.</EmptyState>
      )}

      {blocked && blocked.length > 0 && (
        <section className="px-4 py-3">
          <h3 className="mb-2 text-sm font-semibold uppercase tracking-wide text-text-dim">
            Blocked tasks
          </h3>
          <ul className="list-none">
            {blocked.map((b) => (
              <li key={`${b.queue}/${b.task}`} className="py-1 text-sm">
                <span className="text-text">{b.task}</span>{' '}
                <span className="text-text-dim">— {b.blocked_reason}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {/* The same comparison tables as the Stats screen, reused. */}
      {stats && (
        <>
          <ComparisonTable
            title="By backend"
            label="Backend"
            rows={stats.by_backend}
            labelOf={(b) => b.backend ?? '—'}
          />
          <ComparisonTable
            title="By worker"
            label="Worker"
            rows={stats.by_worker}
            labelOf={(b) => b.worker_id ?? '—'}
          />
        </>
      )}
    </div>
  )
}

function SettingsScreen() {
  const { data, isLoading, error, refetch } = useSettings()
  const [saving, setSaving] = useState(false)
  if (error) return <ErrorState error={error} />
  if (isLoading || !data) return <Spinner />
  return (
    <SettingsEditor
      data={data}
      saving={saving}
      onSave={async (delta) => {
        setSaving(true)
        try {
          await manager.saveSettings(delta)
          await refetch()
        } finally {
          setSaving(false)
        }
      }}
    />
  )
}

function TransportControls() {
  const { data: active } = useActiveState()
  const transport = useTransport()
  const playing = active?.state === 'playing'
  return (
    <div className="flex items-center gap-1">
      <button
        type="button"
        title={playing ? 'Pause' : 'Play'}
        onClick={() =>
          transport.mutate({ action: playing ? 'pause' : 'play' })
        }
        className="inline-flex h-8 w-8 items-center justify-center rounded-md border border-border bg-bg-sunken text-text hover:border-accent"
      >
        {playing ? '❚❚' : '▶'}
      </button>
      <button
        type="button"
        title="Skip"
        onClick={() => transport.mutate({ action: 'skip' })}
        className="inline-flex h-8 w-8 items-center justify-center rounded-md border border-border bg-bg-sunken text-text hover:border-accent"
      >
        ▶❚
      </button>
    </div>
  )
}

export function ManagerApp() {
  const [view, setView] = useState<View>('queue')
  const { data: info } = useInfo()

  // Live convergence: snapshot seeds caches, deltas debounce-invalidate them.
  useSse()

  return (
    <AppShell
      brandName={info?.brand_name ?? 'Nightshift'}
      brandTag="agent task runner"
      logoSrc="./logos/winged-moon.png"
      actions={<TransportControls />}
      nav={
        <>
          <NavTab label="Queue" active={view === 'queue'} onClick={() => setView('queue')} />
          <NavTab label="History" active={view === 'history'} onClick={() => setView('history')} />
          <NavTab label="Stats" active={view === 'stats'} onClick={() => setView('stats')} />
          <NavTab label="Workers" active={view === 'workers'} onClick={() => setView('workers')} />
          <NavTab label="Settings" active={view === 'settings'} onClick={() => setView('settings')} />
        </>
      }
    >
      {view === 'queue' && <QueueScreen />}
      {view === 'history' && <HistoryScreen />}
      {view === 'stats' && <StatsScreen />}
      {view === 'workers' && <WorkersScreen />}
      {view === 'settings' && <SettingsScreen />}
    </AppShell>
  )
}
