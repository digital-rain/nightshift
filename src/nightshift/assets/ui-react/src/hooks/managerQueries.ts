/**
 * Manager-backend (:8800) data hooks. Read hooks are plain useQuery; the SSE
 * convergence hook (useSse) keeps them fresh, so most carry no aggressive
 * polling. Mutations invalidate by the shared qk keys so the UI re-renders.
 */

import {
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query'
import { manager } from '../api/endpoints'
import type {
  QueueSortMode,
  TaskCreate,
  TaskUpdate,
  TransportCommand,
} from '../api/types'
import { qk } from './queryKeys'

export function useInfo() {
  return useQuery({ queryKey: qk.info(), queryFn: manager.info, staleTime: Infinity })
}

export function useQueueItems(queue?: string | null) {
  return useQuery({
    queryKey: qk.queue(queue),
    queryFn: () => manager.queue(queue),
  })
}

/** The focused playlist ({active_playlist}). */
export function useActivePlaylist() {
  return useQuery({ queryKey: qk.active(), queryFn: manager.active })
}

/** Full transport state (state/mode/now_playing/cursor/run_id + per-queue map). */
export function useQueueState() {
  return useQuery({ queryKey: qk.state(), queryFn: manager.state })
}

export function useSort(queue?: string | null) {
  return useQuery({
    queryKey: qk.sort(queue),
    queryFn: () => manager.getSort(queue),
  })
}

export function usePlayPriorities(queue?: string | null) {
  return useQuery({
    queryKey: qk.playPriorities(queue),
    queryFn: () => manager.getPlayPriorities(queue),
  })
}

export function useQueueConfig(queue?: string | null) {
  return useQuery({
    queryKey: qk.queueConfig(queue),
    queryFn: () => manager.getQueueConfig(queue),
  })
}

export function useRepos() {
  return useQuery({ queryKey: qk.repos(), queryFn: manager.repos })
}

export function useDedication(queue?: string | null) {
  return useQuery({
    queryKey: [...qk.dedication(), queue ?? 'main'],
    queryFn: () => manager.getDedication(queue),
  })
}

export function useRunLog(runId: string, task: string, queue?: string | null) {
  return useQuery({
    queryKey: qk.runLog(runId, task),
    queryFn: () => manager.log(runId, task, 0, queue),
  })
}

export function useRuns(queue?: string | null, limit = 200) {
  return useQuery({
    queryKey: qk.runs(queue, limit),
    queryFn: () => manager.runs(queue, limit),
  })
}

export function useWorkers() {
  return useQuery({ queryKey: qk.workers(), queryFn: manager.workers })
}

export function useLeases() {
  return useQuery({ queryKey: qk.leases(), queryFn: manager.leases })
}

export function useBlocked() {
  return useQuery({ queryKey: qk.blocked(), queryFn: manager.blocked })
}

export function useManagerStats() {
  return useQuery({ queryKey: qk.stats(), queryFn: manager.stats })
}

export function useTask(task: string | null, queue?: string | null) {
  return useQuery({
    queryKey: task ? qk.task(task, queue) : qk.taskDefaults(queue),
    queryFn: () => (task ? manager.task(task, queue) : manager.taskDefaults(queue)),
  })
}

export function useSettings() {
  return useQuery({ queryKey: qk.settings(), queryFn: manager.settings })
}

// ----- mutations ----------------------------------------------------------- //

export function useCreateTask() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: TaskCreate) => manager.createTask(body),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.queueAll() }),
  })
}

export function useUpdateTask(queue?: string | null) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ task, body }: { task: string; body: TaskUpdate }) =>
      manager.updateTask(task, body, queue),
    onSuccess: (_data, { task }) => {
      qc.invalidateQueries({ queryKey: qk.task(task, queue) })
      qc.invalidateQueries({ queryKey: qk.queueAll() })
    },
  })
}

export function useDeleteTask(queue?: string | null) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (task: string) => manager.deleteTask(task, queue),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.queueAll() }),
  })
}

export function useTransport() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (cmd: TransportCommand) => manager.transport(cmd),
    // /transport returns the full state payload — seed the state cache so the
    // mini-player + Now screen flip instantly, ahead of SSE convergence.
    onSuccess: (data) => qc.setQueryData(qk.state(), data),
  })
}

export function useReorderQueue(queue?: string | null) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (order: string[]) => manager.reorder(order, queue),
    onSuccess: (data) => qc.setQueryData(qk.queue(queue), data),
  })
}

export function useSetSort(queue?: string | null) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (sort: QueueSortMode) => manager.setSort(sort, queue),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.sort(queue) })
      qc.invalidateQueries({ queryKey: qk.queue(queue) })
    },
  })
}

export function useSetPlayPriorities(queue?: string | null) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (priorities: number[]) =>
      manager.setPlayPriorities(priorities, queue),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: qk.playPriorities(queue) }),
  })
}

export function useSetQueueRepo(queue?: string | null) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (repo: string | null) => manager.setQueueRepo(repo, queue),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.queueConfig(queue) })
      qc.invalidateQueries({ queryKey: qk.repos() })
    },
  })
}

export function useRescanRepos() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: () => manager.rescanRepos(),
    onSuccess: (data) => {
      qc.setQueryData(qk.repos(), data)
      qc.invalidateQueries({ queryKey: qk.queueAll() })
    },
  })
}

export function useSetDedication(queue?: string | null) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (workerIds: string[]) => manager.setDedication(workerIds, queue),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: qk.dedication() }),
  })
}

export function useSetActive() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (playlist: string | null) => manager.setActive(playlist),
    onSuccess: (data) => {
      qc.setQueryData(qk.active(), data)
      // Switching the focused playlist changes the queue + state views.
      qc.invalidateQueries({ queryKey: qk.queueAll() })
      qc.invalidateQueries({ queryKey: qk.state() })
    },
  })
}

export function usePlaylists() {
  return useQuery({ queryKey: qk.playlists(), queryFn: manager.playlists })
}

export function useCreatePlaylist() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (name: string) => manager.createPlaylist(name),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.playlists() }),
  })
}

export function useUpdatePlaylist() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({
      name,
      body,
    }: {
      name: string
      body: import('../api/types').PlaylistUpdate
    }) => manager.updatePlaylist(name, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.playlists() }),
  })
}

export function useDeletePlaylist() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (name: string) => manager.deletePlaylist(name),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.playlists() }),
  })
}

export function useRescanPlaylists() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: () => manager.rescanPlaylists(),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.playlists() }),
  })
}
