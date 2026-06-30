/**
 * RescanButton — a GhostButton that gives the rescan action visible feedback.
 *
 * Rescan endpoints return the refreshed payload but often nothing *looks*
 * different (no repos changed, no playlists added), so a bare button reads as
 * "nothing happened." This wrapper shows a pending label while the request is in
 * flight and a brief "✓ Rescanned" confirmation afterward, so the operator can
 * see the action fired. `onRescan` should return the mutation promise
 * (`mutateAsync()`).
 */

import { useRef, useState } from 'react'
import { GhostButton } from './primitives'

type Phase = 'idle' | 'running' | 'done'

export function RescanButton({
  onRescan,
  title,
  label = 'Rescan',
}: {
  onRescan: () => Promise<unknown>
  title?: string
  label?: string
}) {
  const [phase, setPhase] = useState<Phase>('idle')
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const run = async () => {
    if (phase === 'running') return
    setPhase('running')
    try {
      await onRescan()
      setPhase('done')
      if (timer.current) clearTimeout(timer.current)
      timer.current = setTimeout(() => setPhase('idle'), 1800)
    } catch {
      setPhase('idle')
    }
  }

  return (
    <GhostButton
      onClick={run}
      disabled={phase === 'running'}
      title={title}
      className={phase === 'done' ? 'border-ok text-ok' : undefined}
    >
      {phase === 'running' ? 'Rescanning…' : phase === 'done' ? '✓ Rescanned' : label}
    </GhostButton>
  )
}
