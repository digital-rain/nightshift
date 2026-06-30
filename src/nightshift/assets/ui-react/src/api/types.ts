/**
 * Typed Nightshift API contract.
 *
 * Hand-derived from the three FastAPI backends (manager :8800, worker UI :8810,
 * single-process server :8799). Every shape here corresponds to a JSON payload
 * the backend actually returns or accepts — see docs/REACT_UI.md for the
 * endpoint→type map. Keep this file the single source of truth: components and
 * hooks import from here, never inline ad-hoc shapes.
 *
 * Timestamps are ISO-8601 UTC strings. Monetary values are plain numbers
 * (the backend coerces Decimal → float before serialising).
 */

// --------------------------------------------------------------------------- //
// Enums / unions
// --------------------------------------------------------------------------- //

export type RunStatus =
  | 'running'
  | 'completed'
  | 'error'
  | 'skipped'
  | 'aborted'

export type TransportState = 'idle' | 'playing' | 'paused'
export type TransportMode = 'auto' | 'oneshot' | 'repeat'
export type WorkerStatus = 'idle' | 'busy' | 'offline'
export type LeaseStatus = 'leased' | 'submitted'
export type QueueSortMode = 'manual' | 'priority'
export type BlockedState = 'blocked' | 'quarantined'

/** Priority is a small int, 0 (highest) … 5 (lowest). */
export type Priority = 0 | 1 | 2 | 3 | 4 | 5

// --------------------------------------------------------------------------- //
// Queue / tasks
// --------------------------------------------------------------------------- //

/** A row in the queue list (`GET /api/queue`, `/api/main/tasks`). */
export interface QueueItem {
  task: string
  title: string
  evergreen: boolean
  disabled: boolean
  quarantined: boolean
  completed: boolean
  priority: number
}

/** Task frontmatter as returned by `GET /api/tasks/{task}` (defaults merged). */
export interface TaskFrontmatter {
  model: string | null
  draft: boolean
  automerge: boolean
  priority: number
  disabled?: boolean
  quarantined?: boolean
  completed?: boolean
  evergreen?: boolean
  repo?: string | null
  loop?: boolean | null
  loop_max_iterations?: number | null
}

/** Full task record (`GET /api/tasks/{task}`, `GET /api/task-defaults`). */
export interface TaskDetail {
  /** null for the new-task defaults payload. */
  task: string | null
  title: string
  body: string
  frontmatter: TaskFrontmatter
  /** Raw frontmatter from the file, before defaults are merged. */
  frontmatter_raw: Record<string, unknown>
  evergreen: boolean
  disabled: boolean
  quarantined?: boolean
  completed?: boolean
  /** Models available for this task's queue. */
  model_options: string[]
}

/** Body for `POST /api/tasks`. */
export interface TaskCreate {
  title: string
  text: string
  quarantined?: boolean | null
  repo?: string | null
  loop?: boolean | null
  loop_max_iterations?: number | null
}

/** Body for `PATCH /api/tasks/{task}` — any subset. */
export interface TaskUpdate {
  disabled?: boolean
  quarantined?: boolean
  completed?: boolean
  evergreen?: boolean
  automerge?: boolean
  draft?: boolean
  model?: string | null
  priority?: number
  title?: string
  body?: string
  repo?: string | null
  loop?: boolean | null
  loop_max_iterations?: number | null
}

export interface QueueConfig {
  repo: string | null
  validate_cmd?: string | null
  auto_resolve?: boolean
}

// --------------------------------------------------------------------------- //
// Runs / history
// --------------------------------------------------------------------------- //

/** Manager run record (`GET /api/runs`). The richest run shape. */
export interface Run {
  id: string
  task: string
  queue: string | null
  worker_id: string | null
  backend: string | null
  model: string | null
  status: RunStatus
  phase: string | null
  result_line: string | null
  commit_sha: string | null
  loc: number | null
  remote: string | null
  pushed: boolean | null
  turns: number | null
  input_tokens: number | null
  output_tokens: number | null
  cost_usd: number | null
  failure_kind: string | null
  failure_reason: string | null
  validate_cmd: string | null
  worktree: string | null
  title: string | null
  body: string | null
  started_at: string
  finished_at: string | null
}

/** Worker-UI history row (`GET /api/history` on :8810) — a leaner run view. */
export interface WorkerRun {
  run_id: string
  task: string
  status: string
  result: string | null
  output_lines: number
  started_at: string
  finished_at: string | null
}

/** A log slice (`GET /api/runs/{run_id}/{task}/log`). */
export interface LogSlice {
  text: string
  offset: number
  eof: boolean
}

// --------------------------------------------------------------------------- //
// Workers / leases / blocked
// --------------------------------------------------------------------------- //

export interface Worker {
  id: string
  backend: string
  queues: string[] | null
  priorities: number[] | null
  models: string[]
  mcps: string[]
  status: WorkerStatus
  current_task: string | null
  current_queue: string | null
  current_run_id: string | null
  registered_at: string
  last_checkin_at: string
  last_heartbeat_at: string
  meta: Record<string, unknown>
}

export interface Lease {
  id: string
  task: string
  queue: string
  worker_id: string
  run_id: string | null
  status: LeaseStatus
  model: string | null
  base_ref: string | null
  acquired_at: string
  heartbeat_at: string
  expires_at: string
  released_at: string | null
}

export interface BlockedTask {
  queue: string
  task: string
  state: BlockedState
  blocked_reason: string
  repo: string | null
}

// --------------------------------------------------------------------------- //
// Playlists
// --------------------------------------------------------------------------- //

export interface Playlist {
  name: string
  task_count: number
  disabled: boolean
  /** Present on detail / update responses. */
  repository?: string | null
}

export interface PlaylistUpdate {
  name?: string | null
  repository?: string | null
  disabled?: boolean | null
}

// --------------------------------------------------------------------------- //
// Transport / now-playing
// --------------------------------------------------------------------------- //

/** Per-queue playback state nested in `/api/active` → `queues`. */
export interface QueueState {
  state: TransportState
  mode: TransportMode
  now_playing: string | null
  cursor: number | null
  run_id: string | null
  active_playlist: string | null
  running_playlist: string | null
}

/** Aggregate transport state (`GET /api/active`, `GET /api/state`). */
export interface ActiveState extends QueueState {
  queues: Record<string, Partial<QueueState>>
}

export interface TransportCommand {
  action: 'play' | 'pause' | 'stop' | 'skip' | 'select'
  mode?: TransportMode | null
  task?: string | null
  queue?: string | null
}

// --------------------------------------------------------------------------- //
// Stats
// --------------------------------------------------------------------------- //

/** One aggregation bucket — `overall`, or a row in by_worker/backend/model/queue. */
export interface StatsBucket {
  total_runs: number
  completed: number
  error: number
  aborted?: number
  skipped?: number
  total_turns: number | null
  avg_turns: number | null
  total_cost_usd: number | null
  avg_cost_usd: number | null
  /** Identifier present on grouped rows (e.g. the worker id / model / queue). */
  worker_id?: string
  backend?: string
  model?: string
  queue?: string
  /** LOC + avg-seconds appear in the manager comparison tables. */
  loc?: number | null
  avg_seconds?: number | null
}

/** Manager stats (`GET /api/stats` on :8800). */
export interface ManagerStats {
  overall: StatsBucket
  by_worker: StatsBucket[]
  by_backend: StatsBucket[]
  by_model: StatsBucket[]
  by_queue: StatsBucket[]
}

/** Worker-UI stats (`GET /api/stats` on :8810) — a flat summary. */
export interface WorkerStats {
  total_runs: number
  completed: number
  error: number
  total_turns: number | null
  total_cost_usd: number | null
}

// --------------------------------------------------------------------------- //
// Settings (tier / category / field editor)
// --------------------------------------------------------------------------- //

export type SettingsFieldType =
  | 'int'
  | 'str'
  | 'bool'
  | 'enum'
  | 'duration'
  | 'readonly'
  | 'str_list'
  | 'str_map'
  | 'int_list'

export interface SettingsField {
  key: string
  label: string
  desc: string
  type: SettingsFieldType
  apply: 'restart' | 'live'
  store: string
  default: unknown
  secret: boolean
  stored: unknown
  effective: unknown
  env: string | null
  env_shadowed: boolean
  options: string[] | null
}

export interface SettingsCategory {
  name: string
  fields: SettingsField[]
}

export interface SettingsTier {
  surface: string
  categories: SettingsCategory[]
}

export interface SettingsResponse {
  cursor: number
  tiers: SettingsTier[]
}

/** Response to `PUT /api/settings`. */
export interface SettingsSaveResponse extends SettingsResponse {
  ok: boolean
  errors?: string[]
  applied_live?: string[]
  restart_required?: string[]
}

// --------------------------------------------------------------------------- //
// Info / branding
// --------------------------------------------------------------------------- //

/** Manager / server branding (`GET /api/info` on :8800). */
export interface ManagerInfo {
  brand_name: string
}

/** Worker-UI info (`GET /api/info` on :8810). */
export interface WorkerInfo {
  worker_id: string
  backend: string | null
  queues: string[] | null
  priorities: number[] | null
  models: string[]
  mcps: string[]
  manager_url: string
  worker_url: string | null
  brand_tag: string
  refresh_ms: number
}

/** Worker-UI now-playing (`GET /api/now` on :8810). Empty object when idle. */
export interface WorkerNow {
  run_id?: string | null
  task?: string | null
  status?: string | null
  result?: string | null
  output_lines?: number | null
  started_at?: string | null
  finished_at?: string | null
}

// --------------------------------------------------------------------------- //
// SSE frames (`GET /api/events` on the manager)
// --------------------------------------------------------------------------- //

export interface SseSnapshot {
  type: 'snapshot'
  cursor: number
  workers: Worker[]
  leases: Lease[]
  runs: Run[]
  blocked: BlockedTask[]
}

export type SseEventKind =
  | 'worker_registered'
  | 'queue_changed'
  | 'task_blocked'
  | 'repo_unavailable'
  | 'lease_acquired'
  | 'run_started'
  | 'task_result'
  | (string & {}) // forward-compatible: unknown kinds still type-check

export interface SseDelta {
  type: 'event'
  id: number
  kind: SseEventKind
  queue: string | null
  task: string | null
  run_id: string | null
  payload: Record<string, unknown>
}

export type SseFrame = SseSnapshot | SseDelta
