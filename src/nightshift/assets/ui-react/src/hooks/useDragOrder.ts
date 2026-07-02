/**
 * Native HTML5 drag-to-reorder for the queue list, mirroring the legacy queue's
 * manual ordering. The caller owns the ordered id list and supplies an
 * `onReorder(newOrder)` callback (wired to `manager.reorder`). The hook tracks
 * the dragged id + the current drop target and returns per-row drag handlers
 * plus the live order so the UI can render an optimistic preview while dragging.
 *
 * Dragging is disabled when `enabled` is false (e.g. priority-sort active).
 */

import { useCallback, useState, type DragEvent } from 'react'

export interface DragRowHandlers {
  draggable: boolean
  onDragStart: (e: DragEvent) => void
  onDragOver: (e: DragEvent) => void
  onDrop: (e: DragEvent) => void
  onDragEnd: () => void
  /** True while this row is the active drop target (for a drop-indicator). */
  isOver: boolean
  /** True while this row is the one being dragged. */
  isDragging: boolean
}

export function useDragOrder(
  ids: string[],
  onReorder: (order: string[]) => void,
  enabled = true,
) {
  const [dragId, setDragId] = useState<string | null>(null)
  const [overId, setOverId] = useState<string | null>(null)

  const move = useCallback(
    (from: string, to: string) => {
      if (from === to) return
      const next = [...ids]
      const fromIdx = next.indexOf(from)
      const toIdx = next.indexOf(to)
      if (fromIdx < 0 || toIdx < 0) return
      next.splice(fromIdx, 1)
      next.splice(toIdx, 0, from)
      onReorder(next)
    },
    [ids, onReorder],
  )

  const handlers = useCallback(
    (id: string): DragRowHandlers => ({
      draggable: enabled,
      onDragStart: (e) => {
        if (!enabled) return
        setDragId(id)
        e.dataTransfer.effectAllowed = 'move'
        e.dataTransfer.setData('text/plain', id)
      },
      onDragOver: (e) => {
        if (!enabled || !dragId) return
        e.preventDefault()
        e.dataTransfer.dropEffect = 'move'
        if (overId !== id) setOverId(id)
      },
      onDrop: (e) => {
        if (!enabled || !dragId) return
        e.preventDefault()
        move(dragId, id)
        setDragId(null)
        setOverId(null)
      },
      onDragEnd: () => {
        setDragId(null)
        setOverId(null)
      },
      isOver: overId === id && dragId !== id,
      isDragging: dragId === id,
    }),
    [enabled, dragId, overId, move],
  )

  return { handlers, dragging: dragId !== null }
}
