/**
 * Manager / operator UI — the full music-metaphor surface, ported from the
 * legacy manager UI (iPhone-Music model). Composes the shared component kit
 * against the manager backend (:8800) with live SSE convergence.
 *
 * Chrome:
 *   Top bar  — brand + transport-mode segmented control (oneshot/auto/repeat),
 *              play-priority filter (ALL/P0…P5), + Add, theme toggle, gear menu
 *              (Settings / Workers / Repos).
 *   Bottom   — a mini-player (play/pause · skip · stop) over a tab strip
 *              (Home / Now / Queue / Playlists / History), iPhone-Music style.
 *
 * Screens: Now, Queue, Playlists, History (+ Stats takeover), Settings, Workers,
 * Repos, and the task-detail / new-task takeover. useSse() drives convergence.
 */

import { useState } from 'react'
import { AppShell, NavTab } from '../src/app/AppShell'
import { TaskList } from '../src/components/TaskList'
import { TaskDetail } from '../src/components/TaskDetail'
import { ManagerStatsView } from '../src/components/StatsPage'
import { SettingsEditor } from '../src/components/SettingsEditor'
import { Segmented } from '../src/components/Segmented'
import { RowMenu } from '../src/components/RowMenu'
import { ErrorState, IconButton, Spinner } from '../src/components/primitives'
import {
  ChevronLeftIcon,
  GearIcon,
  HistoryIcon,
  ModeAutoIcon,
  ModeOneshotIcon,
  ModeRepeatIcon,
  NowIcon,
  PauseIcon,
  PlayIcon,
  PlaylistsIcon,
  PlusIcon,
  QueueIcon,
  SkipIcon,
  StopIcon,
} from '../src/components/icons'
import { NowScreen } from './screens/NowScreen'
import { QueueScreen } from './screens/QueueScreen'
import { PlaylistsScreen } from './screens/PlaylistsScreen'
import { ReposScreen } from './screens/ReposScreen'
import { WorkersScreen } from './screens/WorkersScreen'
import { RunDetailScreen } from './screens/RunDetailScreen'
import { runToRow } from '../src/lib/rowAdapters'
import {
  useInfo,
  useManagerStats,
  usePlayPriorities,
  useQueueItems,
  useQueueState,
  useRuns,
  useSettings,
  useSetPlayPriorities,
  useTask,
  useTransport,
  useUpdateTask,
} from '../src/hooks/managerQueries'
import { useSse } from '../src/hooks/useSse'
import { useSettingsSave } from '../src/hooks/useSettingsSave'
import { useTheme } from '../src/hooks/useTheme'
import { manager } from '../src/api/endpoints'
import type { TransportMode } from '../src/api/types'

type View =
  | 'now'
  | 'queue'
  | 'playlists'
  | 'history'
  | 'stats'
  | 'settings'
  | 'workers'
  | 'repos'

// --------------------------------------------------------------------------- //
// Task editor takeover (shared by Now/Queue detail open + the + Add new-task).
// --------------------------------------------------------------------------- //

function TaskEditor({ task, onBack }: { task: string; onBack: () => void }) {
  const { data, isLoading, error } = useTask(task)
  const update = useUpdateTask()
  if (error) return <ErrorState error={error} />
  if (isLoading || !data) return <Spinner />
  return (
    <TaskDetail
      detail={data}
      onBack={onBack}
      saving={update.isPending}
      onSave={(patch) => update.mutate({ task, body: patch }, { onSuccess: onBack })}
    />
  )
}

/** New-task takeover — an empty draft posted via createTask. */
function NewTaskEditor({ onBack }: { onBack: () => void }) {
  const { data, isLoading, error } = useTask(null)
  const [saving, setSaving] = useState(false)
  if (error) return <ErrorState error={error} />
  if (isLoading || !data) return <Spinner />
  return (
    <TaskDetail
      detail={data}
      onBack={onBack}
      saving={saving}
      onSave={async (patch) => {
        setSaving(true)
        try {
          await manager.createTask({
            title: patch.title ?? '',
            text: patch.body ?? '',
          })
          onBack()
        } finally {
          setSaving(false)
        }
      }}
    />
  )
}

// --------------------------------------------------------------------------- //
// History + Stats.
// --------------------------------------------------------------------------- //

function HistoryScreen({ onStats }: { onStats: () => void }) {
  const { data, isLoading, error } = useRuns()
  const [openRun, setOpenRun] = useState<string | null>(null)

  const run = data?.find((r) => r.id === openRun)
  if (run) {
    return <RunDetailScreen run={run} onBack={() => setOpenRun(null)} />
  }

  return (
    <TaskList
      title="History"
      rows={(data ?? []).map(runToRow)}
      isLoading={isLoading}
      error={error}
      emptyMessage="No runs yet."
      onRowClick={setOpenRun}
      actions={
        <button
          type="button"
          onClick={onStats}
          className="inline-flex h-8 items-center rounded-md border border-border bg-transparent px-3 text-[13px] text-text-dim hover:border-accent hover:text-text"
        >
          Stats
        </button>
      }
    />
  )
}

function StatsScreen({ onBack }: { onBack: () => void }) {
  const { data, isLoading, error } = useManagerStats()
  const { data: runs } = useRuns()
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex items-center gap-2 border-b border-border px-4 py-2.5">
        <IconButton title="Back to History" aria-label="Back" onClick={onBack}>
          <ChevronLeftIcon className="h-4 w-4" />
        </IconButton>
        <h2 className="text-base font-semibold text-text">Statistics</h2>
      </div>
      {error ? (
        <ErrorState error={error} />
      ) : isLoading || !data ? (
        <Spinner />
      ) : (
        <ManagerStatsView stats={data} runs={runs} />
      )}
    </div>
  )
}

function SettingsScreen() {
  const { data, isLoading, error, refetch } = useSettings()
  const { save, saving, error: saveError } = useSettingsSave(
    manager.saveSettings,
    refetch,
  )
  if (error) return <ErrorState error={error} />
  if (isLoading || !data) return <Spinner />
  return (
    <SettingsEditor data={data} saving={saving} saveError={saveError} onSave={save} />
  )
}

// --------------------------------------------------------------------------- //
// Top-bar transport chrome.
// --------------------------------------------------------------------------- //

function TransportModeControl() {
  const { data: state } = useQueueState()
  const transport = useTransport()
  const mode = state?.mode ?? 'auto'
  return (
    <Segmented<TransportMode>
      options={[
        { value: 'oneshot', content: <ModeOneshotIcon className="h-[18px] w-[18px]" />, title: '1-shot — run a single task once' },
        { value: 'auto', content: <ModeAutoIcon className="h-[18px] w-[18px]" />, title: 'Play once — run the whole queue one time' },
        { value: 'repeat', content: <ModeRepeatIcon className="h-[18px] w-[18px]" />, title: 'Repeat — loop the queue continuously' },
      ]}
      isActive={(v) => v === mode}
      onSelect={(m) => transport.mutate({ action: 'select', mode: m })}
    />
  )
}

const PRIORITIES = [0, 1, 2, 3, 4, 5] as const

function PriorityFilter() {
  const { data } = usePlayPriorities()
  const setPriorities = useSetPlayPriorities()
  const selected = data?.priorities ?? [...PRIORITIES]
  const all = selected.length === PRIORITIES.length

  const toggle = (p: number | 'all') => {
    if (p === 'all') {
      setPriorities.mutate([...PRIORITIES])
      return
    }
    const next = selected.includes(p as number)
      ? selected.filter((x) => x !== p)
      : [...selected, p as number].sort((a, b) => a - b)
    // Never allow an empty set — fall back to ALL.
    setPriorities.mutate(next.length ? next : [...PRIORITIES])
  }

  return (
    <Segmented<number | 'all'>
      size="sm"
      options={[
        { value: 'all', content: 'ALL', title: 'Play all priorities' },
        ...PRIORITIES.map((p) => ({ value: p, content: `P${p}`, title: `P${p}` })),
      ]}
      isActive={(v) => (v === 'all' ? all : !all && selected.includes(v as number))}
      onSelect={toggle}
    />
  )
}

function MiniPlayer() {
  const { data: state } = useQueueState()
  const transport = useTransport()
  const playing = state?.state === 'playing'
  const idle = state?.state === 'idle'
  return (
    <div className="mx-auto flex w-full max-w-[920px] items-center justify-center gap-5 border-b border-border py-3.5">
      <button
        type="button"
        title={playing ? 'Pause' : 'Play'}
        onClick={() => transport.mutate({ action: playing ? 'pause' : 'play' })}
        className="flex h-14 w-14 items-center justify-center text-accent hover:text-text"
      >
        {playing ? <PauseIcon className="h-7 w-7" /> : <PlayIcon className="h-7 w-7" />}
      </button>
      <button
        type="button"
        title="Skip current task"
        onClick={() => transport.mutate({ action: 'skip' })}
        className="flex h-14 w-14 items-center justify-center text-text hover:text-accent disabled:opacity-35"
        disabled={idle}
      >
        <SkipIcon className="h-6 w-6" />
      </button>
      <button
        type="button"
        title="Stop"
        onClick={() => transport.mutate({ action: 'stop' })}
        className="flex h-14 w-14 items-center justify-center text-text hover:text-accent disabled:opacity-35"
        disabled={idle}
      >
        <StopIcon className="h-6 w-6" />
      </button>
    </div>
  )
}

// --------------------------------------------------------------------------- //
// App.
// --------------------------------------------------------------------------- //

export function ManagerApp() {
  const [view, setView] = useState<View>('now')
  const [openTask, setOpenTask] = useState<string | null>(null)
  const [newTask, setNewTask] = useState(false)
  const { data: info } = useInfo()
  const { data: state } = useQueueState()
  const { data: queue = [] } = useQueueItems()
  const { data: runs = [] } = useRuns()
  const transport = useTransport()
  const [theme, toggleTheme] = useTheme()

  // Live convergence: snapshot seeds caches, deltas debounce-invalidate them.
  useSse()

  const goDetail = (task: string) => {
    setNewTask(false)
    setOpenTask(task)
  }

  // The task-detail / new-task takeover sits above the tab views.
  if (openTask) {
    return <TaskEditor key={openTask} task={openTask} onBack={() => setOpenTask(null)} />
  }
  if (newTask) {
    return <NewTaskEditor onBack={() => setNewTask(false)} />
  }

  const togglePlay = () =>
    transport.mutate({ action: state?.state === 'playing' ? 'pause' : 'play' })

  return (
    <AppShell
      brandName={info?.brand_name ?? 'Nightshift'}
      brandTag={
        state?.active_playlist ? (
          <span className="text-accent">{state.active_playlist}</span>
        ) : (
          'agent task runner'
        )
      }
      logoSrc="./logos/winged-moon.png"
      actions={
        <>
          <TransportModeControl />
          <PriorityFilter />
          <IconButton title="Add a task" aria-label="Add a task" onClick={() => setNewTask(true)}>
            <PlusIcon className="h-4 w-4" />
          </IconButton>
          <IconButton
            title={`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`}
            aria-label="Toggle theme"
            onClick={toggleTheme}
          >
            {theme === 'dark' ? '☾' : '☀'}
          </IconButton>
          <RowMenu
            trigger={<GearIcon className="h-5 w-5" />}
            items={[
              { label: 'Settings…', onClick: () => setView('settings') },
              { label: 'Workers', onClick: () => setView('workers') },
              { label: 'Repos', onClick: () => setView('repos') },
            ]}
          />
        </>
      }
      nav={
        <div className="flex flex-col pb-[env(safe-area-inset-bottom)]">
          <MiniPlayer />
          <div className="flex items-stretch">
            <NavTab label="Now" active={view === 'now'} onClick={() => setView('now')} icon={<NowIcon className="h-[22px] w-[22px]" />} />
            <NavTab label="Queue" active={view === 'queue'} onClick={() => setView('queue')} icon={<QueueIcon className="h-[22px] w-[22px]" />} />
            <NavTab label="Playlists" active={view === 'playlists'} onClick={() => setView('playlists')} icon={<PlaylistsIcon className="h-[22px] w-[22px]" />} />
            <NavTab label="History" active={view === 'history' || view === 'stats'} onClick={() => setView('history')} icon={<HistoryIcon className="h-[22px] w-[22px]" />} />
          </div>
        </div>
      }
    >
      {view === 'now' && (
        <NowScreen
          state={state}
          queue={queue}
          runs={runs}
          onTogglePlay={togglePlay}
          onOpenDetail={goDetail}
          onOpenQueue={() => setView('queue')}
        />
      )}
      {view === 'queue' && <QueueScreen onOpenDetail={goDetail} onAdd={() => setNewTask(true)} />}
      {view === 'playlists' && <PlaylistsScreen />}
      {view === 'history' && <HistoryScreen onStats={() => setView('stats')} />}
      {view === 'stats' && <StatsScreen onBack={() => setView('history')} />}
      {view === 'settings' && <SettingsScreen />}
      {view === 'workers' && <WorkersScreen />}
      {view === 'repos' && <ReposScreen />}
    </AppShell>
  )
}
