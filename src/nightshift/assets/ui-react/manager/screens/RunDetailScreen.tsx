/**
 * Run-detail takeover (manager) — a read-only view of a finished run opened from
 * History, with the reconstructed log viewer (/api/runs/{run}/{task}/log) and a
 * metadata grid + result / failure summary. The back chevron returns to History.
 */

import type { Run } from '../../src/api/types'
import { DetailTakeover } from '../../src/components/DetailTakeover'
import { Expando } from '../../src/components/Expando'
import { PhaseStepper, stepsFromPhase } from '../../src/components/PhaseStepper'
import {
  EmptyState,
  Pill,
  Spinner,
  StatusBadge,
} from '../../src/components/primitives'
import {
  fmtAgo,
  fmtCost,
  fmtDuration,
  fmtInt,
  elapsedSeconds,
} from '../../src/lib/format'
import { useRunLog } from '../../src/hooks/managerQueries'

function Meta({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex gap-2 text-xs">
      <span className="w-20 shrink-0 uppercase tracking-wide text-text-dim">{label}</span>
      <span className="min-w-0 break-all font-mono text-text-dim">{value}</span>
    </div>
  )
}

export function RunDetailScreen({ run, onBack }: { run: Run; onBack: () => void }) {
  const { data: log, isLoading } = useRunLog(run.id, run.task)
  const elapsed = elapsedSeconds(run.started_at, run.finished_at)

  return (
    <DetailTakeover title={run.task} onBack={onBack} backTitle="Back to history">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <StatusBadge status={run.status} />
        {run.model && <Pill tone="accent">{run.model}</Pill>}
        {run.failure_kind && <Pill tone="err">{run.failure_kind}</Pill>}
      </div>

      <PhaseStepper steps={stepsFromPhase(run.phase, run.status)} />

      <div className="mb-3 flex flex-col gap-1">
        {run.worker_id && <Meta label="Worker" value={run.worker_id} />}
        {run.backend && <Meta label="Backend" value={run.backend} />}
        {elapsed != null && <Meta label="Elapsed" value={fmtDuration(elapsed)} />}
        {run.turns != null && <Meta label="Turns" value={fmtInt(run.turns)} />}
        {run.cost_usd != null && <Meta label="Cost" value={fmtCost(run.cost_usd)} />}
        {run.loc != null && <Meta label="LOC" value={fmtInt(run.loc)} />}
        {run.commit_sha && <Meta label="Commit" value={run.commit_sha} />}
        <Meta label="Started" value={fmtAgo(run.started_at)} />
      </div>

      {(run.result_line || run.failure_reason) && (
        <div className="mb-3 rounded-md border border-border bg-bg-sunken px-3 py-2 text-sm text-text">
          {run.result_line || run.failure_reason}
        </div>
      )}

      <Expando caption="Log" defaultOpen>
        {isLoading ? (
          <Spinner />
        ) : log?.text ? (
          <pre className="max-h-[340px] overflow-auto whitespace-pre-wrap rounded-md bg-bg-sunken p-3 font-mono text-xs text-text-dim">
            {log.text}
          </pre>
        ) : (
          <EmptyState>No log recorded.</EmptyState>
        )}
      </Expando>
    </DetailTakeover>
  )
}
