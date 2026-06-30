/**
 * TaskList — generic list-of-tasks container.
 *
 * Renders a header (title + count + optional actions), a query-state-aware body
 * (loading / error / empty / rows), and delegates each row to TaskListItem. It
 * is data-agnostic: callers pass already-adapted TaskRowModels (see
 * lib/rowAdapters.tsx). The manager Queue screen, the manager History screen,
 * and the worker History screen are all thin wrappers over this.
 */

import type { ReactNode } from 'react'
import { TaskListItem, type TaskRowModel } from './TaskListItem'
import { Count, EmptyState, ErrorState, Spinner } from './primitives'

export interface TaskListProps {
  title?: ReactNode
  /** Right-aligned header controls (sort, rescan, +Add …). */
  actions?: ReactNode
  rows: TaskRowModel[]
  isLoading?: boolean
  error?: unknown
  emptyMessage?: ReactNode
  onRowClick?: (id: string) => void
  selectedId?: string | null
  /** Hide the header entirely (when the parent supplies its own chrome). */
  bare?: boolean
}

export function TaskList({
  title,
  actions,
  rows,
  isLoading,
  error,
  emptyMessage = 'Nothing here yet.',
  onRowClick,
  selectedId,
  bare,
}: TaskListProps) {
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {!bare && (title != null || actions != null) && (
        <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-3">
          <h2 className="flex items-center text-sm font-semibold uppercase tracking-wide text-text-dim">
            {title}
            {!isLoading && !error && <Count value={rows.length} />}
          </h2>
          {actions != null && (
            <div className="flex items-center gap-2">{actions}</div>
          )}
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-y-auto">
        {error ? (
          <ErrorState error={error} />
        ) : isLoading ? (
          <Spinner />
        ) : rows.length === 0 ? (
          <EmptyState>{emptyMessage}</EmptyState>
        ) : (
          <ul className="flex list-none flex-col gap-1.5 px-4 py-3">
            {rows.map((m) => (
              <TaskListItem
                key={m.id}
                model={m}
                onClick={onRowClick}
                selected={selectedId === m.id}
              />
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}
