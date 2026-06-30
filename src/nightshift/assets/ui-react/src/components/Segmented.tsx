/**
 * Segmented control — the bordered button row shared by the transport-mode
 * selector (.mode-group / .mode-opt) and the play-priority filter (.play-filter
 * / .pf-opt). Options sit flush in a rounded, hairline-bordered group; the
 * active option(s) get the accent-soft fill + accent text.
 *
 * `multiple` toggles between single-select (mode) and the priority filter's
 * multi-select feel; the parent owns selection state and renders via `active`.
 */

import type { ReactNode } from 'react'
import { cn } from '../lib/cn'

export interface SegmentOption<T extends string | number> {
  value: T
  /** Text (priority filter: "ALL", "P0"…) or an icon (mode glyphs). */
  content: ReactNode
  title?: string
}

export function Segmented<T extends string | number>({
  options,
  isActive,
  onSelect,
  size = 'md',
  className,
}: {
  options: SegmentOption<T>[]
  isActive: (value: T) => boolean
  onSelect: (value: T) => void
  /** 'md' = 32px mode/icon buttons; 'sm' = the tighter priority chips. */
  size?: 'md' | 'sm'
  className?: string
}) {
  return (
    <div
      className={cn(
        'inline-flex h-8 overflow-hidden rounded-md border border-border',
        className,
      )}
      role="group"
    >
      {options.map((opt) => {
        const on = isActive(opt.value)
        return (
          <button
            key={String(opt.value)}
            type="button"
            title={opt.title}
            aria-pressed={on}
            onClick={() => onSelect(opt.value)}
            className={cn(
              'inline-flex h-full items-center justify-center border-l border-border bg-bg-sunken first:border-l-0',
              size === 'md' ? 'w-9' : 'min-w-[30px] px-[7px] text-[11px] font-semibold tnum tracking-[0.02em]',
              on ? 'bg-accent-soft text-accent' : 'text-text-dim hover:text-text',
            )}
          >
            {opt.content}
          </button>
        )
      })}
    </div>
  )
}
