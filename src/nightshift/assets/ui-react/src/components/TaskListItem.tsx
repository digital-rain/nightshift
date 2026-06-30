/**
 * TaskListItem — the generalised "task in a list" row.
 *
 * The manager queue, the manager run history, and the worker history all render
 * a list of task-shaped rows. They differ in *what* they show (a queued task has
 * a priority + flags; a run has a status + cost), but they share the same row
 * skeleton: a leading slot, a primary line (title + identifier), a wrapping set
 * of meta chips, and a trailing actions slot. Each caller maps its own record
 * into this normalised shape via an adapter (see lib/rowAdapters.tsx) so the row
 * itself stays free of any one backend's field names.
 */

import type { ReactNode } from 'react'
import { cn } from '../lib/cn'

export interface TaskRowMeta {
  /** Short label, e.g. "P2", "claude-opus-4-8", "$0.04". */
  label: ReactNode
  title?: string
  /** Dim secondary styling (default) vs. emphasised. */
  emphasis?: boolean
}

export interface TaskRowModel {
  /** Stable key for React + selection. */
  id: string
  /** Primary line — usually the task title. */
  title: ReactNode
  /** Secondary identifier under/after the title (task slug, run id). */
  subtitle?: ReactNode
  /** Leading slot (drag handle, status dot, checkbox). */
  leading?: ReactNode
  /** Trailing slot (badge, kebab menu, transport). */
  trailing?: ReactNode
  /** Meta chips rendered in a wrapping row beneath the title. */
  meta?: TaskRowMeta[]
  /** Dim the whole row (disabled / completed tasks). */
  muted?: boolean
}

export function TaskListItem({
  model,
  onClick,
  selected,
}: {
  model: TaskRowModel
  onClick?: (id: string) => void
  selected?: boolean
}) {
  const clickable = !!onClick
  return (
    <li
      className={cn(
        'flex items-start gap-3 border-b border-border px-4 py-3',
        clickable && 'cursor-pointer hover:bg-bg-elev',
        selected && 'bg-bg-elev',
        model.muted && 'opacity-55',
      )}
      onClick={clickable ? () => onClick!(model.id) : undefined}
    >
      {model.leading != null && (
        <div className="flex shrink-0 items-center pt-0.5">{model.leading}</div>
      )}

      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-2">
          <span className="truncate font-medium text-text">{model.title}</span>
          {model.subtitle != null && (
            <span className="truncate text-xs text-text-dim">
              {model.subtitle}
            </span>
          )}
        </div>

        {model.meta && model.meta.length > 0 && (
          <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1">
            {model.meta.map((m, i) => (
              <span
                key={i}
                title={m.title}
                className={cn(
                  'text-xs tnum',
                  m.emphasis ? 'text-text' : 'text-text-dim',
                )}
              >
                {m.label}
              </span>
            ))}
          </div>
        )}
      </div>

      {model.trailing != null && (
        <div className="flex shrink-0 items-center gap-2">{model.trailing}</div>
      )}
    </li>
  )
}
