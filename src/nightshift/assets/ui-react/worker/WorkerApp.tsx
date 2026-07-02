/**
 * Worker UI app — the worker-local surface, wired to the worker backend (:8810)
 * and composed entirely from the shared component kit. Views:
 *
 *   Now      — the rich live-execution card (queue/repo/phase badges, model /
 *              started / branch / worktree metadata, PhaseStepper, auto-scrolling
 *              live log) or an idle message. Polled via /api/now.
 *   History  — StatTiles header + run list; a run opens a read-only detail.
 *   Stats    — the shared stat tiles + donut charts (no fleet comparisons).
 *   Settings — the shared SettingsEditor over /api/settings.
 */

import { useEffect, useRef, useState } from 'react'
import { AppShell, NavTab } from '../src/app/AppShell'
import { TaskList } from '../src/components/TaskList'
import { WorkerStatsView } from '../src/components/StatsPage'
import { SettingsEditor } from '../src/components/SettingsEditor'
import { DetailTakeover } from '../src/components/DetailTakeover'
import { PhaseStepper, stepsFromPhase } from '../src/components/PhaseStepper'
import { useSettingsSave } from '../src/hooks/useSettingsSave'
import {
  EmptyState,
  ErrorState,
  IconButton,
  Pill,
  Spinner,
} from '../src/components/primitives'
import {
  HistoryIcon,
  NowIcon,
  GearIcon,
} from '../src/components/icons'
import { workerRunToRow } from '../src/lib/rowAdapters'
import { fmtAgo, fmtDuration, fmtInt, elapsedSeconds } from '../src/lib/format'
import { useTheme } from '../src/hooks/useTheme'
import {
  useWorkerHistory,
  useWorkerInfo,
  useWorkerNow,
  useWorkerSettings,
  useWorkerStats,
} from '../src/hooks/workerQueries'
import { workerUi } from '../src/api/endpoints'
import type { WorkerRun } from '../src/api/types'

type View = 'now' | 'history' | 'stats' | 'settings'

/** Metadata row in the Now card (label + value). */
function MetaRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex gap-2 text-xs">
      <span className="w-20 shrink-0 uppercase tracking-wide text-text-dim">{label}</span>
      <span className="min-w-0 break-all font-mono text-text-dim">{value}</span>
    </div>
  )
}

function NowView() {
  const { data: now, isLoading, error } = useWorkerNow()
  const logRef = useRef<HTMLPreElement>(null)
  const logText = (now?.log_tail ?? []).join('\n')

  // Auto-scroll the log to the bottom as new lines arrive.
  useEffect(() => {
    const el = logRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [logText])

  if (error) return <ErrorState error={error} />
  if (isLoading) return <Spinner />

  if (!now || !now.task) {
    return (
      <EmptyState>Idle — waiting for the manager to hand out work.</EmptyState>
    )
  }

  const elapsed = elapsedSeconds(now.started_at, undefined) ?? 0

  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4">
      <div className="rounded-[14px] border border-border bg-bg-elev p-5">
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <span className="text-lg font-semibold text-text">{now.title || now.task}</span>
          <Pill>{now.queue}</Pill>
          {now.repo && <Pill>{now.repo}</Pill>}
          <Pill tone="accent">{now.phase}</Pill>
          <span className="ml-auto text-xs uppercase tracking-wide text-text-dim tnum">
            {fmtDuration(elapsed)}
          </span>
        </div>

        <div className="mb-3 flex flex-col gap-1">
          {now.model && <MetaRow label="Model" value={now.model} />}
          {now.backend && <MetaRow label="Backend" value={now.backend} />}
          {now.started_at && <MetaRow label="Started" value={fmtAgo(now.started_at)} />}
          {now.branch && <MetaRow label="Branch" value={now.branch} />}
          {now.worktree && <MetaRow label="Worktree" value={now.worktree} />}
        </div>

        <PhaseStepper steps={stepsFromPhase(now.phase, 'running')} />

        <pre
          ref={logRef}
          className="mt-2 max-h-[50vh] overflow-auto whitespace-pre-wrap rounded-md bg-bg-sunken p-3 font-mono text-xs text-text-dim"
        >
          {logText || '…'}
        </pre>
      </div>
    </div>
  )
}

function HistoryView({ onStats }: { onStats: () => void }) {
  const { data: runs, isLoading, error } = useWorkerHistory()
  const { data: stats } = useWorkerStats()
  const [open, setOpen] = useState<WorkerRun | null>(null)

  if (open) {
    return <RunDetail run={open} onBack={() => setOpen(null)} />
  }

  const tiles = stats ? (
    <div className="flex gap-4 border-b border-border px-4 py-3 text-sm">
      <span>
        <b className="tnum">{fmtInt(stats.total_runs)}</b>{' '}
        <span className="text-text-dim">runs</span>
      </span>
      <span>
        <b className="tnum text-ok">{fmtInt(stats.completed)}</b>{' '}
        <span className="text-text-dim">done</span>
      </span>
      <span>
        <b className="tnum text-err">{fmtInt(stats.errored)}</b>{' '}
        <span className="text-text-dim">err</span>
      </span>
      <span>
        <b className="tnum">{fmtInt(stats.total_loc)}</b>{' '}
        <span className="text-text-dim">LOC</span>
      </span>
      <button type="button" onClick={onStats} className="ml-auto text-text-dim hover:text-text">
        Stats →
      </button>
    </div>
  ) : null

  const byTask = new Map((runs ?? []).map((r) => [r.run_id, r]))

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {tiles}
      <TaskList
        bare
        rows={(runs ?? []).map(workerRunToRow)}
        isLoading={isLoading}
        error={error}
        emptyMessage="No runs yet."
        onRowClick={(id) => {
          const run = byTask.get(id)
          if (run) setOpen(run)
        }}
      />
    </div>
  )
}

/** Read-only run detail from history. */
function RunDetail({ run, onBack }: { run: WorkerRun; onBack: () => void }) {
  return (
    <DetailTakeover title={run.task} onBack={onBack} backTitle="Back to history">
      <div className="flex flex-col gap-2">
        <MetaRow label="Status" value={run.status} />
        {run.result && <MetaRow label="Result" value={run.result} />}
        <MetaRow label="Lines" value={String(run.output_lines)} />
        {run.started_at && <MetaRow label="Started" value={fmtAgo(run.started_at)} />}
        {run.finished_at && <MetaRow label="Finished" value={fmtAgo(run.finished_at)} />}
      </div>
    </DetailTakeover>
  )
}

function StatsView() {
  const { data, isLoading, error } = useWorkerStats()
  if (error) return <ErrorState error={error} />
  if (isLoading || !data) return <Spinner />
  return <WorkerStatsView stats={data} />
}

function SettingsView() {
  const { data, isLoading, error, refetch } = useWorkerSettings()
  const { save, saving, error: saveError } = useSettingsSave(
    workerUi.saveSettings,
    refetch,
  )
  if (error) return <ErrorState error={error} />
  if (isLoading || !data) return <Spinner />
  return (
    <SettingsEditor data={data} saving={saving} saveError={saveError} onSave={save} />
  )
}

export function WorkerApp() {
  const [view, setView] = useState<View>('now')
  const { data: info } = useWorkerInfo()
  const [theme, toggleTheme] = useTheme()

  return (
    <AppShell
      brandName="Nightshift"
      brandTag={info?.brand_tag ?? 'Nightshift Worker'}
      logoSrc="/shared/logo.png"
      actions={
        <div className="flex items-center gap-2">
          {info && (
            <div className="flex items-center gap-2 text-xs text-text-dim">
              <span>{info.worker_id}</span>
              {info.backend && <Pill>{info.backend}</Pill>}
            </div>
          )}
          <IconButton
            title={`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`}
            aria-label="Toggle theme"
            onClick={toggleTheme}
          >
            {theme === 'dark' ? '☾' : '☀'}
          </IconButton>
        </div>
      }
      nav={
        <div className="mx-auto flex w-full max-w-[920px] items-stretch px-3 pb-[env(safe-area-inset-bottom)]">
          <NavTab label="Now" active={view === 'now'} onClick={() => setView('now')} icon={<NowIcon className="h-[22px] w-[22px]" />} />
          <NavTab
            label="History"
            active={view === 'history' || view === 'stats'}
            onClick={() => setView('history')}
            icon={<HistoryIcon className="h-[22px] w-[22px]" />}
          />
          <NavTab
            label="Settings"
            active={view === 'settings'}
            onClick={() => setView('settings')}
            icon={<GearIcon className="h-[22px] w-[22px]" />}
          />
        </div>
      }
    >
      {view === 'now' && <NowView />}
      {view === 'history' && <HistoryView onStats={() => setView('stats')} />}
      {view === 'stats' && <StatsView />}
      {view === 'settings' && <SettingsView />}
    </AppShell>
  )
}
