/**
 * Repos screen — the legacy #screen-repos. Shows the workspace path, warnings
 * for queues bound to an absent repo, the known-repos set (workspace children
 * with a .git), and per-queue repo bindings (editable). Rescan re-discovers
 * repos and auto-resumes any task paused on a now-present repo.
 */

import type { RepoQueueBinding } from '../../src/api/types'
import {
  Count,
  ErrorState,
  GhostButton,
  Pill,
  Spinner,
} from '../../src/components/primitives'
import { useRepos, useRescanRepos, useSetQueueRepo } from '../../src/hooks/managerQueries'

export function ReposScreen() {
  const { data, isLoading, error } = useRepos()
  const rescan = useRescanRepos()

  if (error) return <ErrorState error={error} />
  if (isLoading || !data) return <Spinner />

  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4">
      <div className="mb-4 flex items-center justify-between">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-text-dim">
          Repos
        </h2>
        <GhostButton onClick={() => rescan.mutate()} title="Re-scan the workspace (resumes paused tasks)">
          Rescan
        </GhostButton>
      </div>

      {data.warnings.length > 0 && (
        <div className="mb-4 rounded-md border-l-4 border-warn bg-bg-elev px-3 py-2 text-sm text-warn">
          {data.warnings.map((w) => (
            <div key={w.queue}>
              Queue <strong>{w.queue}</strong> is bound to absent repo{' '}
              <code className="font-mono">{w.repo}</code>.
            </div>
          ))}
        </div>
      )}

      <div className="mb-4 flex items-center gap-3">
        <span className="text-[11px] uppercase tracking-wide text-text-dim">Workspace</span>
        <code className="font-mono text-sm text-text">{data.workspace}</code>
      </div>

      <h3 className="mb-2 flex items-center text-[11px] font-semibold uppercase tracking-[0.06em] text-text-dim">
        Known repos <Count value={data.repos.length} />
      </h3>
      <ul className="mb-6 list-none">
        {data.repos.map((r) => (
          <li key={r.name} className="flex items-center gap-3 border-b border-border py-2">
            <code className="font-mono font-medium text-text">{r.name}</code>
            <Pill tone={r.available ? 'ok' : 'err'}>{r.available ? 'available' : 'absent'}</Pill>
            {data.tasks_repo === r.name && (
              <span className="text-[11px] uppercase tracking-wide text-text-dim">tasks store</span>
            )}
          </li>
        ))}
        {data.repos.length === 0 && (
          <li className="py-2 text-sm text-text-dim">No git repos found in the workspace.</li>
        )}
      </ul>

      <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-[0.06em] text-text-dim">
        Queue bindings
      </h3>
      <ul className="list-none">
        {data.queues.map((b) => (
          <QueueBindingRow key={b.queue} binding={b} known={data.repos.map((r) => r.name)} />
        ))}
      </ul>
    </div>
  )
}

function QueueBindingRow({
  binding,
  known,
}: {
  binding: RepoQueueBinding
  known: string[]
}) {
  // Queue dedication / repo is set on the per-queue config; bind via setQueueRepo
  // scoped to this queue (null clears the binding).
  const queue = binding.queue === 'main' ? null : binding.queue
  const setRepo = useSetQueueRepo(queue)
  // Preserve an absent-but-configured repo as an option so it isn't dropped.
  const options = [...new Set([...known, ...(binding.repo ? [binding.repo] : [])])]
  return (
    <li className="flex items-center gap-3 border-b border-border py-2">
      <span className="min-w-32 font-medium text-text">{binding.queue}</span>
      {binding.repo && (
        <Pill tone={binding.available ? 'ok' : 'err'}>
          {binding.available ? 'available' : 'absent'}
        </Pill>
      )}
      <select
        className="ml-auto rounded-md border border-border bg-bg-sunken px-2 py-1 text-sm text-text outline-none focus:border-accent"
        value={binding.repo ?? ''}
        onChange={(e) => setRepo.mutate(e.target.value || null)}
      >
        <option value="">— inherit / none —</option>
        {options.map((name) => (
          <option key={name} value={name}>
            {name}
          </option>
        ))}
      </select>
    </li>
  )
}
