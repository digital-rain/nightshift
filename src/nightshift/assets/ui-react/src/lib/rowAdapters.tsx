/**
 * Row adapters — map each backend record shape into the shared TaskRowModel.
 *
 * This is the seam that lets one TaskListItem serve three different lists. Each
 * adapter knows one backend shape (a queued task, a manager run, a worker run)
 * and nothing about layout; the row component knows layout and nothing about the
 * shapes. Add a new list by writing an adapter here, not by branching the row.
 */

import { Pill, StatusBadge } from '../components/primitives'
import type { TaskRowModel } from '../components/TaskListItem'
import type { QueueItem, Run, WorkerRun } from '../api/types'
import {
  elapsedSeconds,
  fmtAgo,
  fmtCost,
  fmtDuration,
  fmtInt,
} from './format'

const PRIORITY_LABEL: Record<number, string> = {
  0: 'P0',
  1: 'P1',
  2: 'P2',
  3: 'P3',
  4: 'P4',
  5: 'P5',
}

/** A queued task (manager Queue screen). */
export function queueItemToRow(item: QueueItem): TaskRowModel {
  const flags: string[] = []
  if (item.evergreen) flags.push('evergreen')
  if (item.quarantined) flags.push('quarantined')
  if (item.completed) flags.push('completed')

  return {
    id: item.task,
    title: item.title || item.task,
    subtitle: item.title ? item.task : undefined,
    muted: item.disabled || item.completed,
    leading: (
      <Pill tone={item.priority <= 1 ? 'warn' : 'neutral'}>
        {PRIORITY_LABEL[item.priority] ?? `P${item.priority}`}
      </Pill>
    ),
    meta: flags.map((f) => ({ label: f })),
  }
}

/** A manager run record (manager History screen) — the richest row. */
export function runToRow(run: Run): TaskRowModel {
  const secs = elapsedSeconds(run.started_at, run.finished_at)
  const meta: TaskRowModel['meta'] = []
  if (run.model) meta.push({ label: run.model })
  if (run.worker_id) meta.push({ label: run.worker_id, title: 'worker' })
  if (secs != null) meta.push({ label: fmtDuration(secs), title: 'elapsed' })
  if (run.turns != null) meta.push({ label: `${run.turns} turns` })
  if (run.cost_usd != null)
    meta.push({ label: fmtCost(run.cost_usd), title: 'cost' })
  if (run.loc != null) meta.push({ label: `${fmtInt(run.loc)} LOC` })
  meta.push({ label: fmtAgo(run.started_at) })
  if (run.failure_reason)
    meta.push({ label: run.failure_reason, emphasis: true })

  return {
    id: run.id,
    title: run.title || run.task,
    subtitle: run.title ? run.task : undefined,
    trailing: <StatusBadge status={run.status} />,
    meta,
  }
}

/** A worker-UI history row (worker History screen) — the lean run view. */
export function workerRunToRow(run: WorkerRun): TaskRowModel {
  const secs = elapsedSeconds(run.started_at, run.finished_at)
  const meta: TaskRowModel['meta'] = []
  if (secs != null) meta.push({ label: fmtDuration(secs), title: 'elapsed' })
  meta.push({ label: `${fmtInt(run.output_lines)} lines` })
  meta.push({ label: fmtAgo(run.started_at) })
  if (run.result) meta.push({ label: run.result, emphasis: true })

  return {
    id: run.run_id,
    title: run.task,
    subtitle: run.run_id,
    trailing: <StatusBadge status={run.status} />,
    meta,
  }
}
