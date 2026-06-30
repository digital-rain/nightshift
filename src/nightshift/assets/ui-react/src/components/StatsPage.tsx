/**
 * Shared statistics surface.
 *
 * Both the manager and worker UIs show run statistics. They generalise to two
 * pieces: a row of headline tiles (the legacy .stats-row) and zero or more
 * comparison tables (the legacy .cmp-table — manager only). This file exports
 * the building blocks plus two thin compositions (ManagerStatsView /
 * WorkerStatsView) so each surface just feeds its own stats payload.
 */

import type { ReactNode } from 'react'
import type { ManagerStats, StatsBucket, WorkerStats } from '../api/types'
import { fmtCost, fmtInt } from '../lib/format'
import { EmptyState } from './primitives'

// --------------------------------------------------------------------------- //
// Headline tiles.
// --------------------------------------------------------------------------- //

export interface StatTile {
  num: ReactNode
  label: string
  tone?: 'ok' | 'err' | 'warn' | 'neutral'
}

const TILE_TONE = {
  ok: 'text-ok',
  err: 'text-err',
  warn: 'text-warn',
  neutral: 'text-text',
} as const

export function StatTiles({ tiles }: { tiles: StatTile[] }) {
  return (
    <div className="flex flex-wrap gap-4 px-4 py-4">
      {tiles.map((t, i) => (
        <div
          key={i}
          className="flex min-w-24 flex-col rounded-md border border-border bg-bg-elev px-4 py-3"
        >
          <span
            className={`text-2xl font-semibold tnum ${TILE_TONE[t.tone ?? 'neutral']}`}
          >
            {t.num}
          </span>
          <span className="text-xs uppercase tracking-wide text-text-dim">
            {t.label}
          </span>
        </div>
      ))}
    </div>
  )
}

// --------------------------------------------------------------------------- //
// Comparison table (.cmp-table) — manager by_worker / by_backend / by_model / by_queue.
// --------------------------------------------------------------------------- //

export interface ComparisonColumn {
  header: string
  /** Cell value extractor; receives a bucket row. */
  cell: (b: StatsBucket) => ReactNode
  /** Right-align numeric columns (default true except the first). */
  numeric?: boolean
}

export function ComparisonTable({
  title,
  rows,
  label,
  labelOf,
}: {
  title: string
  rows: StatsBucket[]
  /** Header for the identifier column (e.g. "Worker", "Model"). */
  label: string
  /** Identifier extractor for the first column. */
  labelOf: (b: StatsBucket) => ReactNode
}) {
  if (rows.length === 0) return null
  const cols: ComparisonColumn[] = [
    { header: 'Runs', cell: (b) => fmtInt(b.total_runs) },
    { header: 'Done', cell: (b) => fmtInt(b.completed) },
    { header: 'Err', cell: (b) => fmtInt(b.error) },
    { header: 'LOC', cell: (b) => fmtInt(b.loc) },
    { header: 'Avg s', cell: (b) => (b.avg_seconds == null ? '—' : b.avg_seconds.toFixed(0)) },
    { header: 'Turns', cell: (b) => fmtInt(b.total_turns) },
    { header: 'Cost', cell: (b) => fmtCost(b.total_cost_usd) },
  ]

  return (
    <section className="px-4 py-3">
      <h3 className="mb-2 text-sm font-semibold uppercase tracking-wide text-text-dim">
        {title}
      </h3>
      <table className="w-full border-collapse text-sm tnum">
        <thead>
          <tr className="border-b border-border text-left text-text-dim">
            <th className="py-1.5 pr-3 font-medium">{label}</th>
            {cols.map((c) => (
              <th key={c.header} className="py-1.5 pr-3 text-right font-medium">
                {c.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((b, i) => (
            <tr key={i} className="border-b border-border/60">
              <td className="py-1.5 pr-3 text-text">{labelOf(b)}</td>
              {cols.map((c) => (
                <td key={c.header} className="py-1.5 pr-3 text-right text-text-dim">
                  {c.cell(b)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}

// --------------------------------------------------------------------------- //
// Compositions.
// --------------------------------------------------------------------------- //

function overallTiles(o: StatsBucket): StatTile[] {
  return [
    { num: fmtInt(o.total_runs), label: 'runs' },
    { num: fmtInt(o.completed), label: 'completed', tone: 'ok' },
    { num: fmtInt(o.error), label: 'errored', tone: 'err' },
    { num: o.avg_turns == null ? '—' : o.avg_turns.toFixed(1), label: 'avg turns' },
    { num: fmtCost(o.total_cost_usd), label: 'total cost' },
  ]
}

export function ManagerStatsView({ stats }: { stats: ManagerStats }) {
  if (stats.overall.total_runs === 0) return <EmptyState>No runs yet.</EmptyState>
  return (
    <div className="min-h-0 flex-1 overflow-y-auto">
      <StatTiles tiles={overallTiles(stats.overall)} />
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
      <ComparisonTable
        title="By model"
        label="Model"
        rows={stats.by_model}
        labelOf={(b) => b.model ?? '—'}
      />
      <ComparisonTable
        title="By queue"
        label="Queue"
        rows={stats.by_queue}
        labelOf={(b) => b.queue ?? 'main'}
      />
    </div>
  )
}

export function WorkerStatsView({ stats }: { stats: WorkerStats }) {
  if (stats.total_runs === 0) return <EmptyState>No runs yet.</EmptyState>
  const tiles: StatTile[] = [
    { num: fmtInt(stats.total_runs), label: 'runs' },
    { num: fmtInt(stats.completed), label: 'completed', tone: 'ok' },
    { num: fmtInt(stats.error), label: 'errored', tone: 'err' },
    { num: fmtInt(stats.total_turns), label: 'turns' },
    { num: fmtCost(stats.total_cost_usd), label: 'total cost' },
  ]
  return (
    <div className="min-h-0 flex-1 overflow-y-auto">
      <StatTiles tiles={tiles} />
    </div>
  )
}
