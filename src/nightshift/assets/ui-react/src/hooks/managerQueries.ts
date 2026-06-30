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
import type { TaskCreate, TaskUpdate, TransportCommand } from '../api/types'
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

export function useActiveState() {
  return useQuery({ queryKey: qk.active(), queryFn: manager.active })
}

export function usePlaylists() {
  return useQuery({ queryKey: qk.playlists(), queryFn: manager.playlists })
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
    onSuccess: (data) => qc.setQueryData(qk.active(), data),
  })
}

export function useReorderQueue(queue?: string | null) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (order: string[]) => manager.reorder(order, queue),
    onSuccess: (data) => qc.setQueryData(qk.queue(queue), data),
  })
}
