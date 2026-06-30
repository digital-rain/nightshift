/**
 * Phase stepper — the Worker → Validate → Commit progression, ported from the
 * legacy .stepper / .step / .step-dot / .step-bar. Each step is grey (pending),
 * blue + pulsing (active), green (done), or red (failed). The connecting bars
 * fill green once the step before them is done.
 *
 * Shared by the manager Now screen, the task-detail takeover, and the worker
 * Now card. Animation keyframes (`pulse`) live in theme.css.
 */

import { cn } from '../lib/cn'

export type PhaseState = 'pending' | 'active' | 'done' | 'failed'

export interface Step {
  label: string
  state: PhaseState
}

/** The canonical three phases. */
export const PHASES = ['Worker', 'Validate', 'Commit'] as const

const DOT_TONE: Record<PhaseState, string> = {
  pending: 'border-border bg-transparent',
  active: 'border-accent bg-accent animate-[ns-pulse_1.2s_ease-in-out_infinite]',
  done: 'border-ok bg-ok',
  failed: 'border-err bg-err',
}

const LABEL_TONE: Record<PhaseState, string> = {
  pending: 'text-text-dim',
  active: 'text-accent',
  done: 'text-ok',
  failed: 'text-err',
}

export function PhaseStepper({ steps }: { steps: Step[] }) {
  return (
    <div className="my-5 flex items-center">
      {steps.map((step, i) => (
        <div key={step.label} className="flex flex-1 items-center last:flex-none">
          <div className={cn('flex items-center gap-2', LABEL_TONE[step.state])}>
            <span
              className={cn(
                'h-2.5 w-2.5 rounded-full border-2',
                DOT_TONE[step.state],
              )}
            />
            <span className="text-[13px] font-semibold">{step.label}</span>
          </div>
          {i < steps.length - 1 && (
            <span
              className={cn(
                'mx-2.5 h-0.5 flex-1 rounded-sm',
                step.state === 'done' ? 'bg-ok' : 'bg-border',
              )}
            />
          )}
        </div>
      ))}
    </div>
  )
}

/**
 * Derive the three-step model from a run's phase string + status. Mirrors the
 * legacy mapping: the named phase is "active" (or "failed" on error), earlier
 * phases are "done", later ones "pending"; a completed run marks all done.
 */
export function stepsFromPhase(
  phase: string | null | undefined,
  status?: string | null,
): Step[] {
  const order = ['worker', 'validate', 'commit']
  const failed = status === 'error' || status === 'aborted'
  const completed = status === 'completed'
  // Normalise some known phase aliases onto the three canonical buckets.
  const norm = (phase ?? 'worker').toLowerCase()
  let activeIdx = order.indexOf(norm)
  if (activeIdx < 0) activeIdx = norm === 'diff' || norm === 'code' ? 0 : 0
  return PHASES.map((label, i) => {
    let state: PhaseState
    if (completed) state = 'done'
    else if (i < activeIdx) state = 'done'
    else if (i === activeIdx) state = failed ? 'failed' : 'active'
    else state = 'pending'
    return { label, state }
  })
}
