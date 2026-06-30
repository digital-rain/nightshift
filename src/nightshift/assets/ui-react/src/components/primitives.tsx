/**
 * Foundational UI primitives, ported from the legacy style.css component classes
 * (.pill, .abtn / .tbtn, .ghost-btn, .empty, .count, status colours). Shared by
 * both the manager and worker surfaces. Styling uses the Tailwind tokens defined
 * in theme.css (bg-bg-elev, text-text-dim, border-border, …) so dark/light theme
 * switching is automatic.
 */

import type { ButtonHTMLAttributes, ReactNode } from 'react'
import { cn } from '../lib/cn'
import type { RunStatus } from '../api/types'

// --------------------------------------------------------------------------- //
// Pill — the small rounded status/label chip (.pill).
// --------------------------------------------------------------------------- //

export type PillTone = 'neutral' | 'accent' | 'ok' | 'err' | 'warn'

const PILL_TONE: Record<PillTone, string> = {
  neutral: 'bg-bg-sunken text-text-dim border-border',
  accent: 'bg-accent-soft text-accent border-accent-soft',
  ok: 'bg-bg-sunken text-ok border-border',
  err: 'bg-bg-sunken text-err border-border',
  warn: 'bg-bg-sunken text-warn border-border',
}

export function Pill({
  children,
  tone = 'neutral',
  title,
  className,
}: {
  children: ReactNode
  tone?: PillTone
  title?: string
  className?: string
}) {
  return (
    <span
      title={title}
      className={cn(
        'inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide',
        PILL_TONE[tone],
        className,
      )}
    >
      {children}
    </span>
  )
}

// --------------------------------------------------------------------------- //
// StatusBadge — maps a RunStatus to a toned Pill (shared by run rows + now view).
// --------------------------------------------------------------------------- //

const STATUS_TONE: Record<string, PillTone> = {
  running: 'accent',
  completed: 'ok',
  error: 'err',
  aborted: 'err',
  skipped: 'warn',
}

export function StatusBadge({ status }: { status: RunStatus | string }) {
  return <Pill tone={STATUS_TONE[status] ?? 'neutral'}>{status}</Pill>
}

// --------------------------------------------------------------------------- //
// Buttons — icon button (.abtn / .tbtn) and ghost button (.ghost-btn).
// --------------------------------------------------------------------------- //

type BtnProps = ButtonHTMLAttributes<HTMLButtonElement>

export function IconButton({ className, ...props }: BtnProps) {
  return (
    <button
      type="button"
      {...props}
      className={cn(
        'inline-flex h-8 min-w-9 items-center justify-center rounded-md border border-border bg-bg-sunken px-2 text-sm text-text',
        'hover:border-accent disabled:cursor-default disabled:opacity-40',
        className,
      )}
    />
  )
}

export function GhostButton({ className, ...props }: BtnProps) {
  return (
    <button
      type="button"
      {...props}
      className={cn(
        'inline-flex h-8 items-center rounded-md border border-border bg-transparent px-3 text-[13px] text-text-dim',
        'hover:border-accent hover:text-text disabled:cursor-default disabled:opacity-40',
        className,
      )}
    />
  )
}

export function PrimaryButton({ className, ...props }: BtnProps) {
  return (
    <button
      type="button"
      {...props}
      className={cn(
        'inline-flex h-8 items-center rounded-md border border-accent bg-accent-soft px-3 text-[13px] font-semibold text-accent',
        'hover:brightness-110 disabled:cursor-default disabled:opacity-40',
        className,
      )}
    />
  )
}

// --------------------------------------------------------------------------- //
// EmptyState (.empty) and Count badge (.count).
// --------------------------------------------------------------------------- //

export function EmptyState({ children }: { children: ReactNode }) {
  return (
    <p className="px-4 py-8 text-center text-sm text-text-dim">{children}</p>
  )
}

export function Count({ value }: { value: number | string }) {
  return (
    <span className="ml-1 rounded-full bg-bg-sunken px-2 py-0.5 text-[11px] text-text-dim tnum">
      {value}
    </span>
  )
}

// --------------------------------------------------------------------------- //
// Spinner / loading + error fallbacks for query states.
// --------------------------------------------------------------------------- //

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-2 px-4 py-8 text-sm text-text-dim">
      <span className="h-3 w-3 animate-spin rounded-full border-2 border-border border-t-accent" />
      {label ?? 'Loading…'}
    </div>
  )
}

export function ErrorState({ error }: { error: unknown }) {
  const msg = error instanceof Error ? error.message : String(error)
  return (
    <p className="px-4 py-8 text-center text-sm text-err">
      Failed to load — {msg}
    </p>
  )
}
