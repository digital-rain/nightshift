/**
 * Centralised TanStack Query key factory. Keys are arrays so partial keys can
 * invalidate whole families (e.g. qk.runsAll() invalidates every queue's runs).
 * The SSE hook and mutation hooks both invalidate by these keys — keep them here
 * so there is exactly one spelling of each.
 */

export const qk = {
  // manager
  info: () => ['info'] as const,
  active: () => ['active'] as const,

  queueAll: () => ['queue'] as const,
  queue: (queue?: string | null) => ['queue', queue ?? 'main'] as const,

  task: (task: string, queue?: string | null) =>
    ['task', queue ?? 'main', task] as const,
  taskDefaults: (queue?: string | null) =>
    ['task-defaults', queue ?? 'main'] as const,

  playlists: () => ['playlists'] as const,

  runsAll: () => ['runs'] as const,
  runs: (queue?: string | null, limit = 200) =>
    ['runs', queue ?? 'main', limit] as const,

  workers: () => ['workers'] as const,
  leases: () => ['leases'] as const,
  blocked: () => ['blocked'] as const,
  stats: () => ['stats'] as const,
  models: (queue?: string | null) => ['models', queue ?? 'main'] as const,

  settings: () => ['settings'] as const,

  // worker UI
  wInfo: () => ['w-info'] as const,
  wNow: () => ['w-now'] as const,
  wHistory: (limit = 200) => ['w-history', limit] as const,
  wStats: () => ['w-stats'] as const,
  wScanQueues: () => ['w-scan-queues'] as const,
  wSettings: () => ['w-settings'] as const,
}
