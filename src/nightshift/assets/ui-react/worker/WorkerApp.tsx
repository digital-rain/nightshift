/**
 * Worker UI app — the smaller of the two surfaces, and a complete demonstration
 * of the shared component kit wired to the worker backend (:8810). Three views:
 *
 *   Now      — current task (polled via /api/now)
 *   History  — run list → shared TaskList, rows via workerRunToRow adapter,
 *              with the shared StatTiles header; opens the shared Stats view
 *   Settings — shared SettingsEditor over /api/settings
 *
 * Everything below the shell is shared code; this file only chooses *which*
 * shared piece to show and feeds it worker-shaped data.
 */

import { useState } from 'react'
import { AppShell, NavTab } from '../src/app/AppShell'
import { TaskList } from '../src/components/TaskList'
import { WorkerStatsView } from '../src/components/StatsPage'
import { SettingsEditor } from '../src/components/SettingsEditor'
import { useSettingsSave } from '../src/hooks/useSettingsSave'
import {
  ErrorState,
  Pill,
  Spinner,
  StatusBadge,
} from '../src/components/primitives'
import { workerRunToRow } from '../src/lib/rowAdapters'
import { fmtAgo, fmtInt } from '../src/lib/format'
import {
  useWorkerHistory,
  useWorkerInfo,
  useWorkerNow,
  useWorkerSettings,
  useWorkerStats,
} from '../src/hooks/workerQueries'
import { workerUi } from '../src/api/endpoints'

type View = 'now' | 'history' | 'stats' | 'settings'

function NowView() {
  const { data: now, isLoading, error } = useWorkerNow()
  if (error) return <ErrorState error={error} />
  if (isLoading) return <Spinner />

  const active = now && now.task
  if (!active) {
    return (
      <p className="px-4 py-10 text-center text-sm text-text-dim">
        Idle — waiting for the manager to hand out work.
      </p>
    )
  }

  return (
    <div className="px-4 py-4">
      <div className="rounded-md border border-border bg-bg-elev p-4">
        <div className="mb-2 flex items-center gap-2">
          <span className="font-medium text-text">{now.task}</span>
          {now.status && <StatusBadge status={now.status} />}
        </div>
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-text-dim">
          {now.output_lines != null && (
            <span>{fmtInt(now.output_lines)} lines</span>
          )}
          {now.started_at && <span>started {fmtAgo(now.started_at)}</span>}
          {now.result && <span className="text-text">{now.result}</span>}
        </div>
      </div>
    </div>
  )
}

function HistoryView({ onStats }: { onStats: () => void }) {
  const { data: runs, isLoading, error } = useWorkerHistory()
  const { data: stats } = useWorkerStats()

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
        <b className="tnum text-err">{fmtInt(stats.error)}</b>{' '}
        <span className="text-text-dim">err</span>
      </span>
      <button
        type="button"
        onClick={onStats}
        className="ml-auto text-text-dim hover:text-text"
      >
        Stats →
      </button>
    </div>
  ) : null

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {tiles}
      <TaskList
        bare
        rows={(runs ?? []).map(workerRunToRow)}
        isLoading={isLoading}
        error={error}
        emptyMessage="No runs yet."
      />
    </div>
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

  return (
    <AppShell
      brandName="Nightshift"
      brandTag={info?.brand_tag ?? 'Nightshift Worker'}
      logoSrc="/shared/logo.png"
      actions={
        info && (
          <div className="flex items-center gap-2 text-xs text-text-dim">
            <span>{info.worker_id}</span>
            {info.backend && <Pill>{info.backend}</Pill>}
          </div>
        )
      }
      nav={
        <>
          <NavTab label="Now" active={view === 'now'} onClick={() => setView('now')} />
          <NavTab
            label="History"
            active={view === 'history' || view === 'stats'}
            onClick={() => setView('history')}
          />
          <NavTab
            label="Settings"
            active={view === 'settings'}
            onClick={() => setView('settings')}
          />
        </>
      }
    >
      {view === 'now' && <NowView />}
      {view === 'history' && <HistoryView onStats={() => setView('stats')} />}
      {view === 'stats' && <StatsView />}
      {view === 'settings' && <SettingsView />}
    </AppShell>
  )
}
