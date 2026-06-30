/**
 * Now screen — the manager's live-execution view, ported from the legacy
 * #screen-now. Shows the running task as a card (title + .md filename, model
 * badge, play/pause, a 1s elapsed ticker, the phase stepper, and a collapsible
 * live-log panel) or an idle hero (big play + next-task suggestion) when nothing
 * is running.
 *
 * now_playing from /api/state is just the task id; title/model are looked up
 * from the queue list, and the live log + phase come from the latest matching
 * run (the SSE-converged runs cache).
 */

import { useEffect, useState } from 'react'
import type { ActiveState, QueueItem, Run } from '../../src/api/types'
import { PhaseStepper, stepsFromPhase } from '../../src/components/PhaseStepper'
import { Expando } from '../../src/components/Expando'
import { Pill, EmptyState } from '../../src/components/primitives'
import { PlayIcon, PauseIcon, ChevronRightIcon } from '../../src/components/icons'
import { elapsedSeconds, fmtDuration } from '../../src/lib/format'

/** A 1s ticker driving the live elapsed clock. */
function useNowTick(active: boolean): number {
  const [, setN] = useState(0)
  useEffect(() => {
    if (!active) return
    const id = setInterval(() => setN((n) => n + 1), 1000)
    return () => clearInterval(id)
  }, [active])
  return Date.now()
}

export function NowScreen({
  state,
  queue,
  runs,
  onTogglePlay,
  onOpenDetail,
  onOpenQueue,
}: {
  state: ActiveState | undefined
  queue: QueueItem[]
  runs: Run[]
  onTogglePlay: () => void
  onOpenDetail: (task: string) => void
  onOpenQueue: () => void
}) {
  const playing = state?.state === 'playing'
  const nowTask = state?.now_playing ?? null
  useNowTick(playing && !!nowTask)

  if (!nowTask) {
    // Idle hero — big play + the next queued task.
    const next = queue[0]
    return (
      <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-4 px-6 text-center">
        <button
          type="button"
          onClick={onTogglePlay}
          disabled={!next}
          title={next ? 'Play' : 'Queue is empty'}
          className="flex h-20 w-20 items-center justify-center rounded-full border border-border bg-bg-sunken text-accent hover:border-accent disabled:opacity-40"
        >
          <PlayIcon className="h-9 w-9" />
        </button>
        <div>
          <div className="text-lg font-semibold text-text">Idle</div>
          <div className="mt-1 text-sm text-text-dim">
            {next ? <>Next up: {next.title}</> : 'Queue is empty'}
          </div>
        </div>
        {queue.length > 0 && (
          <button
            type="button"
            onClick={onOpenQueue}
            className="mt-2 inline-flex items-center gap-1 text-sm text-accent hover:underline"
          >
            Up next · {queue.length} queued
            <ChevronRightIcon className="h-4 w-4" />
          </button>
        )}
      </div>
    )
  }

  // Running — find the live run + queue metadata for this task.
  const run = runs.find((r) => r.task === nowTask && r.status === 'running')
  const item = queue.find((q) => q.task === nowTask)
  const title = run?.title ?? item?.title ?? nowTask
  const model = run?.model ?? null
  const phase = run?.phase ?? 'worker'
  const elapsed = run ? (elapsedSeconds(run.started_at, undefined) ?? 0) : 0

  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4">
      <div className="rounded-[14px] border border-border bg-bg-elev p-5">
        <div className="flex items-start justify-between gap-4">
          <button
            type="button"
            onClick={() => onOpenDetail(nowTask)}
            className="min-w-0 text-left"
          >
            <div className="truncate text-lg font-semibold text-text">{title}</div>
            <div className="mt-0.5 font-mono text-xs text-text-dim">{nowTask}.md</div>
          </button>
          <div className="flex shrink-0 items-center gap-2">
            {model && <Pill tone="accent">{model}</Pill>}
            <button
              type="button"
              onClick={onTogglePlay}
              title={playing ? 'Pause' : 'Play'}
              className="flex h-10 w-10 items-center justify-center rounded-full border border-border bg-bg-sunken text-accent hover:border-accent"
            >
              {playing ? <PauseIcon className="h-5 w-5" /> : <PlayIcon className="h-5 w-5" />}
            </button>
          </div>
        </div>

        <div className="mt-1 text-xs uppercase tracking-wide text-text-dim tnum">
          {phase} · {fmtDuration(elapsed)}
        </div>

        <PhaseStepper steps={stepsFromPhase(phase, run?.status)} />

        <Expando caption="Log" defaultOpen>
          <RunLogTail runId={run?.id ?? null} task={nowTask} />
        </Expando>
      </div>
    </div>
  )
}

/**
 * Live log tail for the running task. The manager reconstructs a run's log from
 * its persisted task_log events via /api/runs/{run}/{task}/log; here we just
 * fetch the current slice (SSE invalidation refreshes it). Auto-scrolls to the
 * bottom as new lines arrive.
 */
function RunLogTail({ runId, task }: { runId: string | null; task: string }) {
  const [text, setText] = useState('')
  useEffect(() => {
    if (!runId) return
    let stop = false
    const load = async () => {
      try {
        const res = await fetch(
          `/api/runs/${encodeURIComponent(runId)}/${encodeURIComponent(task)}/log`,
        )
        if (!res.ok) return
        const data = (await res.json()) as { text: string }
        if (!stop) setText(data.text ?? '')
      } catch {
        /* ignore transient fetch errors */
      }
    }
    load()
    const id = setInterval(load, 2000)
    return () => {
      stop = true
      clearInterval(id)
    }
  }, [runId, task])

  if (!runId) return <EmptyState>No log yet.</EmptyState>
  return (
    <pre className="max-h-[340px] overflow-auto whitespace-pre-wrap rounded-md bg-bg-sunken p-3 font-mono text-xs text-text-dim">
      {text || '…'}
    </pre>
  )
}
