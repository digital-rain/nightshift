/**
 * Collapsible panel — ported from the legacy `.xpanel` expando used for the
 * Log / Result / Run-details sections of the task detail takeover. A clickable
 * head row with an uppercase caption + a chevron that rotates 90° on open; the
 * body slides out below. An optional right-aligned accessory (e.g. the brief's
 * Markdown|Preview toggle) sits in the head.
 */

import { useState, type ReactNode } from 'react'
import { cn } from '../lib/cn'
import { ChevronRightIcon } from './icons'

export function Expando({
  caption,
  defaultOpen = false,
  accessory,
  children,
}: {
  caption: string
  defaultOpen?: boolean
  accessory?: ReactNode
  children: ReactNode
}) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <div className="overflow-hidden rounded-[10px] border border-border">
      <div className="flex items-stretch">
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
          className="flex flex-1 items-center gap-2 px-3 py-2.5 text-left text-text-dim hover:text-accent focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent"
        >
          <ChevronRightIcon
            className={cn(
              'h-4 w-4 transition-transform duration-150',
              open && 'rotate-90',
            )}
          />
          <span className="text-[11px] font-semibold uppercase tracking-[0.06em]">
            {caption}
          </span>
        </button>
        {accessory && (
          <div className="flex items-center pr-3">{accessory}</div>
        )}
      </div>
      {open && (
        <div className="flex flex-col gap-2 border-t border-border px-3 py-3">
          {children}
        </div>
      )}
    </div>
  )
}
