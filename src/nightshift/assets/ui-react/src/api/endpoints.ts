/**
 * Typed endpoint functions, grouped by resource. Thin wrappers over `api` that
 * pin the request/response types from types.ts. Hooks (see hooks/) call these;
 * components call hooks, not these directly.
 *
 * Manager-only endpoints and worker-UI-only endpoints are split into the
 * `manager` and `workerUi` namespaces because the two backends are distinct
 * surfaces (different ports, different shapes for /api/info, /api/now, /api/stats).
 */

import { api, withQueue } from './client'
import type {
  ActiveState,
  BlockedTask,
  Lease,
  LogSlice,
  ManagerInfo,
  ManagerStats,
  Playlist,
  PlaylistUpdate,
  QueueConfig,
  QueueItem,
  Run,
  SettingsResponse,
  SettingsSaveResponse,
  TaskCreate,
  TaskDetail,
  TaskUpdate,
  TransportCommand,
  Worker,
  WorkerInfo,
  WorkerNow,
  WorkerRun,
  WorkerStats,
} from './types'

// --------------------------------------------------------------------------- //
// Manager backend (:8800) — also shared by the single-process server (:8799).
// --------------------------------------------------------------------------- //

export const manager = {
  info: () => api.get<ManagerInfo>('/info'),

  // Queue + tasks
  queue: (queue?: string | null) =>
    api.get<QueueItem[]>(withQueue('/queue', queue)),
  mainTasks: () => api.get<QueueItem[]>('/main/tasks'),
  reorder: (order: string[], queue?: string | null) =>
    api.put<QueueItem[]>(withQueue('/queue/order', queue), { order }),
  getSort: (queue?: string | null) =>
    api.get<{ sort: 'manual' | 'priority' }>(withQueue('/queue/sort', queue)),
  setSort: (sort: 'manual' | 'priority', queue?: string | null) =>
    api.put(withQueue('/queue/sort', queue), { sort }),
  getPlayPriorities: (queue?: string | null) =>
    api.get<{ priorities: number[] }>(withQueue('/queue/play-priorities', queue)),
  setPlayPriorities: (priorities: number[], queue?: string | null) =>
    api.put(withQueue('/queue/play-priorities', queue), { priorities }),
  getQueueConfig: (queue?: string | null) =>
    api.get<QueueConfig>(withQueue('/queue/config', queue)),
  setQueueRepo: (repo: string | null, queue?: string | null) =>
    api.put<{ repo: string | null }>(withQueue('/queue/repo', queue), { repo }),

  task: (task: string, queue?: string | null) =>
    api.get<TaskDetail>(withQueue(`/tasks/${encodeURIComponent(task)}`, queue)),
  taskDefaults: (queue?: string | null) =>
    api.get<TaskDetail>(withQueue('/task-defaults', queue)),
  createTask: (body: TaskCreate) => api.post<TaskDetail>('/tasks', body),
  updateTask: (task: string, body: TaskUpdate, queue?: string | null) =>
    api.patch<TaskDetail>(
      withQueue(`/tasks/${encodeURIComponent(task)}`, queue),
      body,
    ),
  deleteTask: (task: string, queue?: string | null) =>
    api.del<{ task: string }>(
      withQueue(`/tasks/${encodeURIComponent(task)}`, queue),
    ),

  // Playlists
  playlists: () => api.get<Playlist[]>('/playlists'),
  createPlaylist: (name: string) => api.post<Playlist>('/playlists', { name }),
  playlist: (name: string) =>
    api.get<Playlist>(`/playlists/${encodeURIComponent(name)}`),
  updatePlaylist: (name: string, body: PlaylistUpdate) =>
    api.put<Playlist>(`/playlists/${encodeURIComponent(name)}`, body),
  deletePlaylist: (name: string) =>
    api.del<{ removed: string }>(`/playlists/${encodeURIComponent(name)}`),
  rescanPlaylists: () => api.post<Playlist[]>('/playlists/rescan'),

  // Runs / history
  runs: (queue?: string | null, limit = 200) =>
    api.get<Run[]>(withQueue(`/runs?limit=${limit}`, queue)),
  log: (runId: string, task: string, offset = 0, queue?: string | null) =>
    api.get<LogSlice>(
      withQueue(
        `/runs/${encodeURIComponent(runId)}/${encodeURIComponent(task)}/log?offset=${offset}`,
        queue,
      ),
    ),

  // Workers / leases / blocked / stats / models
  workers: () => api.get<Worker[]>('/workers'),
  leases: () => api.get<Lease[]>('/leases'),
  blocked: () => api.get<BlockedTask[]>('/blocked'),
  stats: () => api.get<ManagerStats>('/stats'),
  models: (queue?: string | null) =>
    api.get<{ models: string[] }>(withQueue('/models', queue)),

  // Transport / now
  active: () => api.get<ActiveState>('/active'),
  state: () => api.get<ActiveState>('/state'),
  transport: (cmd: TransportCommand) =>
    api.post<ActiveState>('/transport', cmd),

  // Settings
  settings: () => api.get<SettingsResponse>('/settings'),
  saveSettings: (delta: unknown) =>
    api.put<SettingsSaveResponse>('/settings', delta),
}

// --------------------------------------------------------------------------- //
// Worker UI backend (:8810).
// --------------------------------------------------------------------------- //

export const workerUi = {
  info: () => api.get<WorkerInfo>('/info'),
  now: () => api.get<WorkerNow>('/now'),
  history: (limit = 200) => api.get<WorkerRun[]>(`/history?limit=${limit}`),
  stats: () => api.get<WorkerStats>('/stats'),
  scanQueues: () => api.get<{ queues: string[] }>('/scan-queues'),
  settings: () => api.get<SettingsResponse>('/settings'),
  saveSettings: (delta: unknown) =>
    api.put<SettingsSaveResponse>('/settings', delta),
}
