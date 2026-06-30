/**
 * Worker-UI backend (:8810) data hooks. The worker UI has no SSE; it polls. The
 * poll interval comes from /api/info.refresh_ms (falling back to 3000ms, as the
 * legacy worker app.js did), so we fetch info first and thread its cadence into
 * the now/history polls.
 */

import { useQuery } from '@tanstack/react-query'
import { workerUi } from '../api/endpoints'
import { qk } from './queryKeys'

const DEFAULT_REFRESH_MS = 3000

export function useWorkerInfo() {
  return useQuery({
    queryKey: qk.wInfo(),
    queryFn: workerUi.info,
    staleTime: Infinity,
  })
}

/** Resolve the worker's UI poll cadence from /api/info (or the 3s fallback). */
export function useRefreshMs(): number {
  const { data } = useWorkerInfo()
  return data?.refresh_ms ?? DEFAULT_REFRESH_MS
}

export function useWorkerNow() {
  const refetchInterval = useRefreshMs()
  return useQuery({
    queryKey: qk.wNow(),
    queryFn: workerUi.now,
    refetchInterval,
  })
}

export function useWorkerHistory(limit = 200) {
  const refetchInterval = useRefreshMs()
  return useQuery({
    queryKey: qk.wHistory(limit),
    queryFn: () => workerUi.history(limit),
    refetchInterval,
  })
}

export function useWorkerStats() {
  return useQuery({ queryKey: qk.wStats(), queryFn: workerUi.stats })
}

export function useWorkerSettings() {
  return useQuery({ queryKey: qk.wSettings(), queryFn: workerUi.settings })
}
