/**
 * Shared statistics surface.
 *
 * Both the manager and worker UIs show run statistics. They generalise to:
 *   - a row of headline tiles / cards (the legacy .stat-cards),
 *   - SVG donut charts with legends (the legacy proportionDonut), and
 *   - zero or more comparison tables (the legacy .cmp-table — manager only).
 *
 * This file exports the building blocks plus two thin compositions
 * (ManagerStatsView / WorkerStatsView) so each surface feeds its own payload.
 * Donut segments for failure modes are derived from the run list (the backend
 * /api/stats has no failure breakdown — the legacy UI computed it client-side).
 */

import type { ReactNode } from 'react'
import type { ManagerStats, Run, StatsBucket, WorkerStats } from '../api/types'
import { fmtCost, fmtInt } from '../lib/format'
import { EmptyState } from './primitives'

// --------------------------------------------------------------------------- //
// Headline tiles / cards.
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
// Donut chart (ported from app.js proportionDonut) — dependency-free SVG.
// --------------------------------------------------------------------------- //

/** The legacy chart palette (ok/err semantic + c0..c5 categorical hues). */
export const CHART_COLORS = [
  'var(--color-accent)',
  'var(--color-warn)',
  '#b07aff',
  '#4dbdcc',
  '#ff8767',
  '#96d6a0',
] as const

export interface DonutSegment {
  label: string
  count: number
  color: string
  /** Override the legend value text (e.g. "$0.42" instead of the raw count). */
  display?: string
}

const R = 76
const STROKE = 10.8
const CIRC = 2 * Math.PI * R

/**
 * A donut split into proportional arcs via stroke-dasharray, with a centred
 * label and a legend below. `centerText` overrides the default "first-segment
 * percentage" center value.
 */
export function DonutChart({
  title,
  segments,
  centerText,
}: {
  title: string
  segments: DonutSegment[]
  centerText?: string
}) {
  const total = segments.reduce((s, seg) => s + seg.count, 0)
  let offset = 0
  const pct = total ? Math.round((segments[0].count / total) * 100) : 0

  return (
    <div className="flex flex-col items-center">
      <div className="mb-2 text-[11px] uppercase tracking-[0.06em] text-text-dim">
        {title}
      </div>
      <svg viewBox="0 0 200 200" width="160" height="160" className="block">
        <g transform="rotate(-90 100 100)">
          <circle
            cx="100"
            cy="100"
            r={R}
            fill="none"
            strokeWidth={STROKE}
            stroke="var(--color-border)"
          />
          {segments.map((seg, i) => {
            if (!seg.count) return null
            const dash = (seg.count / total) * CIRC
            const el = (
              <circle
                key={i}
                cx="100"
                cy="100"
                r={R}
                fill="none"
                strokeWidth={STROKE}
                stroke={seg.color}
                strokeDasharray={`${dash} ${CIRC - dash}`}
                strokeDashoffset={-offset}
              >
                <title>{`${seg.label}: ${seg.display ?? seg.count}`}</title>
              </circle>
            )
            offset += dash
            return el
          })}
        </g>
        <text
          x="100"
          y="112"
          textAnchor="middle"
          className="fill-text font-bold"
          style={{ fontSize: 36 }}
        >
          {centerText ?? `${pct}%`}
        </text>
      </svg>
      <div className="mt-2 flex flex-col gap-1.5 text-xs text-text-dim">
        {segments.map((seg, i) => {
          const segPct = total ? Math.round((seg.count / total) * 100) : 0
          return (
            <span key={i} className="inline-flex items-center gap-1.5">
              <span
                className="inline-block h-2.5 w-2.5 rounded-sm"
                style={{ background: seg.color }}
              />
              {seg.label} {seg.display ?? seg.count} ({segPct}%)
            </span>
          )
        })}
      </div>
    </div>
  )
}

/** Friendly labels for classified failure kinds (ported from app.js). */
export const FAILURE_LABELS: Record<string, string> = {
  merge_conflict: 'merge conflict',
  merge_rejected: 'merge rejected',
  validation_error: 'validation',
  worker_error: 'worker error',
  worker_launch: 'worker launch',
  timeout: 'timeout',
  aborted: 'aborted',
  no_changes: 'no changes',
}

/** Build a failure-modes donut from a run list (backend has no breakdown). */
export function failureSegments(runs: Run[]): DonutSegment[] {
  const counts = new Map<string, number>()
  for (const r of runs) {
    if (r.status === 'error' || r.status === 'aborted') {
      const kind = r.failure_kind || 'worker_error'
      counts.set(kind, (counts.get(kind) ?? 0) + 1)
    }
  }
  return [...counts.entries()].map(([kind, count], i) => ({
    label: FAILURE_LABELS[kind] ?? kind,
    count,
    color: CHART_COLORS[i % CHART_COLORS.length],
  }))
}

/** The donut row shared by the manager and worker stats screens. */
export function StatCharts({
  overall,
  byModel,
  runs,
}: {
  overall: StatsBucket
  byModel?: StatsBucket[]
  runs?: Run[]
}) {
  const failed = overall.total_runs - overall.completed
  const charts: ReactNode[] = []

  charts.push(
    <DonutChart
      key="outcomes"
      title="Outcomes"
      segments={[
        { label: 'Completed', count: overall.completed, color: 'var(--color-ok)' },
        { label: 'Failed', count: failed, color: 'var(--color-err)' },
      ]}
    />,
  )

  if (runs && runs.length) {
    const segs = failureSegments(runs)
    if (segs.length) {
      charts.push(<DonutChart key="failures" title="Failure modes" segments={segs} />)
    }
  }

  if (byModel && byModel.length) {
    charts.push(
      <DonutChart
        key="model-usage"
        title="Model usage"
        segments={byModel.map((b, i) => ({
          label: b.model ?? '—',
          count: b.total_runs,
          color: CHART_COLORS[i % CHART_COLORS.length],
        }))}
      />,
    )
    const costTotal = byModel.reduce((s, b) => s + (b.total_cost_usd ?? 0), 0)
    if (costTotal > 0) {
      charts.push(
        <DonutChart
          key="model-cost"
          title="Model cost"
          centerText={fmtCost(costTotal)}
          segments={byModel.map((b, i) => ({
            label: b.model ?? '—',
            count: b.total_cost_usd ?? 0,
            display: fmtCost(b.total_cost_usd ?? 0),
            color: CHART_COLORS[i % CHART_COLORS.length],
          }))}
        />,
      )
    }
  }

  return (
    <div className="flex flex-wrap items-start justify-center gap-x-10 gap-y-6 px-4 py-4">
      {charts}
    </div>
  )
}

// --------------------------------------------------------------------------- //
// Comparison table (.cmp-table) — by_worker / by_backend / by_model / by_queue.
// --------------------------------------------------------------------------- //

interface CmpColumn {
  header: string
  cell: (b: StatsBucket) => ReactNode
}

const CMP_COLUMNS: CmpColumn[] = [
  { header: 'Runs', cell: (b) => fmtInt(b.total_runs) },
  { header: 'Done', cell: (b) => fmtInt(b.completed) },
  { header: 'Err', cell: (b) => fmtInt(b.error) },
  { header: 'LOC', cell: (b) => fmtInt(b.loc ?? 0) },
  { header: 'Avg s', cell: (b) => (b.avg_seconds == null ? '—' : b.avg_seconds.toFixed(0)) },
  { header: 'Turns', cell: (b) => fmtInt(b.total_turns ?? 0) },
  { header: 'Cost', cell: (b) => fmtCost(b.total_cost_usd) },
]

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
  return (
    <section className="px-4 py-3">
      <h3 className="mb-2 text-sm font-semibold uppercase tracking-wide text-text-dim">
        {title}
      </h3>
      <table className="w-full border-collapse text-sm tnum">
        <thead>
          <tr className="border-b border-border text-left text-text-dim">
            <th className="py-1.5 pr-3 font-medium">{label}</th>
            {CMP_COLUMNS.map((c) => (
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
              {CMP_COLUMNS.map((c) => (
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

/** Manager's four comparison tables — also reused by the Workers screen. */
export function ComparisonTables({ stats }: { stats: ManagerStats }) {
  return (
    <>
      <ComparisonTable title="By backend" label="Backend" rows={stats.by_backend} labelOf={(b) => b.backend ?? '—'} />
      <ComparisonTable title="By worker" label="Worker" rows={stats.by_worker} labelOf={(b) => b.worker_id ?? '—'} />
      <ComparisonTable title="By model" label="Model" rows={stats.by_model} labelOf={(b) => b.model ?? '—'} />
      <ComparisonTable title="By queue" label="Queue" rows={stats.by_queue} labelOf={(b) => b.queue ?? 'main'} />
    </>
  )
}

// --------------------------------------------------------------------------- //
// Compositions.
// --------------------------------------------------------------------------- //

function overallTiles(o: StatsBucket): StatTile[] {
  const successRate = o.total_runs ? Math.round((o.completed / o.total_runs) * 100) : 0
  return [
    { num: fmtInt(o.total_runs), label: 'tasks' },
    { num: fmtInt(o.completed), label: 'completed', tone: 'ok' },
    { num: `${successRate}%`, label: 'success rate' },
    { num: fmtInt(o.loc ?? 0), label: 'LOC' },
    { num: o.avg_turns == null ? '—' : o.avg_turns.toFixed(1), label: 'avg turns' },
    { num: fmtCost(o.total_cost_usd), label: 'total cost' },
  ]
}

export function ManagerStatsView({
  stats,
  runs,
}: {
  stats: ManagerStats
  /** Run list for the failure-mode donut (optional). */
  runs?: Run[]
}) {
  if (stats.overall.total_runs === 0) return <EmptyState>No runs yet.</EmptyState>
  return (
    <div className="min-h-0 flex-1 overflow-y-auto">
      <StatTiles tiles={overallTiles(stats.overall)} />
      <StatCharts overall={stats.overall} byModel={stats.by_model} runs={runs} />
      <ComparisonTables stats={stats} />
    </div>
  )
}

export function WorkerStatsView({
  stats,
  runs,
}: {
  stats: WorkerStats
  runs?: Run[]
}) {
  if (stats.total_runs === 0) return <EmptyState>No runs yet.</EmptyState>
  const tiles: StatTile[] = [
    { num: fmtInt(stats.total_runs), label: 'tasks' },
    { num: fmtInt(stats.completed), label: 'completed', tone: 'ok' },
    { num: fmtInt(stats.errored), label: 'errored', tone: 'err' },
    { num: fmtInt(stats.total_loc), label: 'LOC' },
  ]
  // Synthesize an overall bucket for the outcomes donut.
  const overall: StatsBucket = {
    total_runs: stats.total_runs,
    completed: stats.completed,
    error: stats.errored,
    total_turns: null,
    avg_turns: null,
    total_cost_usd: null,
    avg_cost_usd: null,
    loc: stats.total_loc,
  }
  return (
    <div className="min-h-0 flex-1 overflow-y-auto">
      <StatTiles tiles={tiles} />
      <StatCharts overall={overall} runs={runs} />
    </div>
  )
}
