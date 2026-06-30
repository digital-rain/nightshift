/**
 * Queue screen — the legacy #screen-queue UP-NEXT list, ported with its chrome:
 * the active-queue name + an Add menu, a count + a sort toggle (manual ↔
 * priority), drag-to-reorder (native HTML5 DnD; disabled while priority-sorted),
 * and a per-row actions menu (info, play next/last, enable/disable, remove,
 * set priority). Rows reuse queueItemToRow for their badges/flags.
 */

import { useEffect, useState } from 'react'
import type { QueueItem } from '../../src/api/types'
import { queueItemToRow } from '../../src/lib/rowAdapters'
import { TaskListItem } from '../../src/components/TaskListItem'
import { RowMenu } from '../../src/components/RowMenu'
import {
  Count,
  EmptyState,
  ErrorState,
  Spinner,
} from '../../src/components/primitives'
import { GripIcon, SortIcon } from '../../src/components/icons'
import { cn } from '../../src/lib/cn'
import { useDragOrder } from '../../src/hooks/useDragOrder'
import {
  useDeleteTask,
  useQueueItems,
  useReorderQueue,
  useSetSort,
  useSort,
  useTransport,
  useUpdateTask,
} from '../../src/hooks/managerQueries'

export function QueueScreen({
  onOpenDetail,
  onAdd,
}: {
  onOpenDetail: (task: string) => void
  onAdd: () => void
}) {
  const { data: items, isLoading, error } = useQueueItems()
  const { data: sortData } = useSort()
  const setSort = useSetSort()
  const reorder = useReorderQueue()
  const transport = useTransport()
  const update = useUpdateTask()
  const del = useDeleteTask()

  const prioritySort = sortData?.sort === 'priority'

  // Local optimistic order so a drag re-renders immediately; reset when the
  // server list changes (SSE convergence or a different queue).
  const [order, setOrder] = useState<string[]>([])
  useEffect(() => {
    setOrder((items ?? []).map((i) => i.task))
  }, [items])

  const byTask = new Map((items ?? []).map((i) => [i.task, i]))
  const ordered = order
    .map((t) => byTask.get(t))
    .filter((i): i is QueueItem => !!i)

  const { handlers } = useDragOrder(
    order,
    (next) => {
      setOrder(next)
      reorder.mutate(next)
    },
    !prioritySort,
  )

  if (error) return <ErrorState error={error} />

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* Queue chrome — name + Add menu. */}
      <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-text-dim">
          Main queue
        </h2>
        <RowMenu
          trigger="+ Add"
          items={[{ label: 'New task…', onClick: onAdd }]}
        />
      </div>

      {/* UP NEXT header + count + sort toggle. */}
      <div className="flex items-center gap-2 px-4 pt-3">
        <h3 className="text-[11px] font-semibold uppercase tracking-[0.06em] text-text-dim">
          Up next
        </h3>
        <Count value={ordered.length} />
        <button
          type="button"
          title={prioritySort ? 'Sorting by priority' : 'Sort by priority'}
          aria-pressed={prioritySort}
          onClick={() => setSort.mutate(prioritySort ? 'manual' : 'priority')}
          className={cn(
            'ml-auto inline-flex h-7 w-7 items-center justify-center rounded-md border border-border',
            prioritySort ? 'bg-accent-soft text-accent' : 'text-text-dim hover:text-text',
          )}
        >
          <SortIcon className="h-4 w-4" />
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {isLoading ? (
          <Spinner />
        ) : ordered.length === 0 ? (
          <EmptyState>No pending tasks.</EmptyState>
        ) : (
          <ul className="flex list-none flex-col gap-1.5 px-4 py-3">
            {ordered.map((item) => {
              const h = handlers(item.task)
              const row = queueItemToRow(item)
              return (
                <div
                  key={item.task}
                  draggable={h.draggable}
                  onDragStart={h.onDragStart}
                  onDragOver={h.onDragOver}
                  onDrop={h.onDrop}
                  onDragEnd={h.onDragEnd}
                  className={cn(
                    'rounded-[10px] transition-opacity',
                    h.isDragging && 'opacity-40',
                    h.isOver && 'ring-2 ring-accent ring-offset-2 ring-offset-bg',
                  )}
                >
                  <TaskListItem
                    model={{
                      ...row,
                      leading: !prioritySort ? (
                        <span className="cursor-grab text-text-dim" title="Drag to reorder">
                          <GripIcon className="h-4 w-4" />
                        </span>
                      ) : undefined,
                      trailing: (
                        <RowMenu
                          trigger="⋯"
                          items={[
                            { label: 'Open / edit', onClick: () => onOpenDetail(item.task) },
                            {
                              label: 'Play next',
                              onClick: () => transport.mutate({ action: 'select', task: item.task }),
                            },
                            {
                              label: item.disabled ? 'Enable' : 'Disable',
                              onClick: () =>
                                update.mutate({ task: item.task, body: { disabled: !item.disabled } }),
                            },
                            {
                              label: 'Remove',
                              tone: 'danger',
                              onClick: () => del.mutate(item.task),
                            },
                          ]}
                        />
                      ),
                    }}
                    onClick={() => onOpenDetail(item.task)}
                  />
                </div>
              )
            })}
          </ul>
        )}
      </div>
    </div>
  )
}
