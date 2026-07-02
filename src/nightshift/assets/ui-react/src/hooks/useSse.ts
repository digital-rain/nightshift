/**
 * Manager SSE convergence hook.
 *
 * Ports the legacy manager-events.js + the app.js 250ms debounce-refetch: open
 * an EventSource on /api/events, hand the initial snapshot to the caller, and on
 * each delta frame debounce-invalidate the affected TanStack Query caches so all
 * open tabs converge on live state (reorders, worker check-ins, leases, runs).
 *
 * The backend emits two frame types (see api/types.ts SseFrame):
 *   { type: "snapshot", cursor, workers, leases, runs, blocked }  on connect
 *   { type: "event", id, kind, queue, task, run_id, payload }     per change
 * plus `: keep-alive` comment heartbeats (ignored by EventSource.onmessage).
 */

import { useEffect, useRef } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import type { SseFrame, SseSnapshot } from '../api/types'
import { qk } from './queryKeys'

export interface UseSseOptions {
  /** Called once per connection with the initial snapshot. */
  onSnapshot?: (snap: SseSnapshot) => void
  /** Debounce window for cache invalidation after a delta. Default 250ms. */
  debounceMs?: number
  enabled?: boolean
}

export function useSse(opts: UseSseOptions = {}): void {
  const { onSnapshot, debounceMs = 250, enabled = true } = opts
  const qc = useQueryClient()
  // Keep the latest onSnapshot without re-opening the stream on every render.
  const onSnapshotRef = useRef(onSnapshot)
  onSnapshotRef.current = onSnapshot

  useEffect(() => {
    if (!enabled) return

    const es = new EventSource('/api/events')
    let timer: ReturnType<typeof setTimeout> | null = null

    const flush = () => {
      timer = null
      // The cheap, correct default: re-fetch the live surfaces. Matches the
      // legacy scheduleRefresh() which reloaded runs/queue/workers wholesale.
      qc.invalidateQueries({ queryKey: qk.workers() })
      qc.invalidateQueries({ queryKey: qk.leases() })
      qc.invalidateQueries({ queryKey: qk.blocked() })
      qc.invalidateQueries({ queryKey: qk.active() })
      qc.invalidateQueries({ queryKey: qk.state() })
      qc.invalidateQueries({ queryKey: qk.runsAll() })
      qc.invalidateQueries({ queryKey: qk.queueAll() })
    }

    const scheduleFlush = () => {
      if (timer) return
      timer = setTimeout(flush, debounceMs)
    }

    es.onmessage = (evt: MessageEvent<string>) => {
      let frame: SseFrame
      try {
        frame = JSON.parse(evt.data) as SseFrame
      } catch {
        return
      }
      if (frame.type === 'snapshot') {
        onSnapshotRef.current?.(frame)
        // Seed the caches whose keys the snapshot maps to exactly, so first
        // paint is immediate.
        qc.setQueryData(qk.workers(), frame.workers)
        qc.setQueryData(qk.leases(), frame.leases)
        qc.setQueryData(qk.blocked(), frame.blocked)
        // The snapshot also carries runs, but the runs cache is keyed by
        // (queue, limit) and the snapshot is only the main-queue latest slice —
        // it doesn't map to a specific key. And queue/active aren't in the
        // snapshot at all. A snapshot arrives on every (re)connect, so anything
        // that changed during a disconnect must be re-pulled: invalidate those
        // families here rather than waiting for the next delta. Without this, a
        // dropped-then-restored connection leaves runs/queue/transport stale
        // while the link looks healthy.
        qc.invalidateQueries({ queryKey: qk.runsAll() })
        qc.invalidateQueries({ queryKey: qk.queueAll() })
        qc.invalidateQueries({ queryKey: qk.active() })
        qc.invalidateQueries({ queryKey: qk.state() })
      } else if (frame.type === 'event') {
        scheduleFlush()
      }
    }

    // EventSource auto-reconnects on transient errors; nothing to do here beyond
    // letting it retry. A hard failure simply stops live updates — polling on
    // the individual queries still keeps data fresh.
    es.onerror = () => {
      /* allow native retry */
    }

    return () => {
      if (timer) clearTimeout(timer)
      es.close()
    }
  }, [qc, debounceMs, enabled])
}
