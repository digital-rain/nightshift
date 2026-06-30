/**
 * Workers screen — the legacy #screen-workers. The registered worker fleet
 * (backend, status, current task), blocked/quarantined tasks, a per-queue
 * dedication editor (bind a queue to specific workers), and the four comparison
 * tables (by backend / worker / model / queue) reused from the Stats surface.
 */

import { useState } from 'react'
import { ComparisonTables } from '../../src/components/StatsPage'
import {
  EmptyState,
  ErrorState,
  GhostButton,
  Pill,
  Spinner,
} from '../../src/components/primitives'
import {
  useBlocked,
  useDedication,
  useManagerStats,
  usePlaylists,
  useSetDedication,
  useWorkers,
} from '../../src/hooks/managerQueries'

export function WorkersScreen() {
  const { data: workers, isLoading, error } = useWorkers()
  const { data: blocked } = useBlocked()
  const { data: stats } = useManagerStats()
  const { data: playlists } = usePlaylists()

  if (error) return <ErrorState error={error} />
  if (isLoading) return <Spinner />

  return (
    <div className="min-h-0 flex-1 overflow-y-auto">
      <ul className="list-none">
        {(workers ?? []).map((w) => (
          <li key={w.id} className="flex items-center gap-3 border-b border-border px-4 py-3">
            <span className="font-medium text-text">{w.id}</span>
            <Pill tone={w.status === 'busy' ? 'accent' : w.status === 'offline' ? 'neutral' : 'ok'}>
              {w.status}
            </Pill>
            <span className="text-xs text-text-dim">{w.backend}</span>
            {w.current_task && (
              <span className="ml-auto text-xs text-text-dim">{w.current_task}</span>
            )}
          </li>
        ))}
      </ul>
      {(workers ?? []).length === 0 && <EmptyState>No workers checked in.</EmptyState>}

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

      {/* Queue dedication editor — bind each queue to specific worker ids. */}
      <section className="px-4 py-3">
        <h3 className="mb-1 text-sm font-semibold uppercase tracking-wide text-text-dim">
          Queue dedication
        </h3>
        <p className="mb-2 text-xs text-text-dim">
          Bind a queue to specific worker(s): its tasks are then offered only to those
          workers. Leave blank for any matching worker.
        </p>
        <ul className="list-none">
          {(playlists ?? []).map((p) => (
            <DedicationRow key={p.name} queue={p.name} />
          ))}
        </ul>
      </section>

      {stats && <ComparisonTables stats={stats} />}
    </div>
  )
}

function DedicationRow({ queue }: { queue: string }) {
  const target = queue === 'library' || queue === 'main' ? null : queue
  const { data } = useDedication(target)
  const setDedication = useSetDedication(target)
  const [value, setValue] = useState<string | null>(null)
  // Seed from the server value until the user edits.
  const text = value ?? (data?.worker_ids ?? []).join(', ')
  return (
    <li className="flex items-center gap-3 py-1.5">
      <span className="min-w-32 text-sm text-text">{queue}</span>
      <input
        type="text"
        value={text}
        onChange={(e) => setValue(e.target.value)}
        placeholder="any worker"
        className="flex-1 rounded-md border border-border bg-bg-sunken px-2 py-1 text-sm text-text outline-none focus:border-accent"
      />
      <GhostButton
        onClick={() => {
          const ids = text
            .split(',')
            .map((s) => s.trim())
            .filter(Boolean)
          setDedication.mutate(ids)
          setValue(null)
        }}
      >
        Save
      </GhostButton>
    </li>
  )
}
