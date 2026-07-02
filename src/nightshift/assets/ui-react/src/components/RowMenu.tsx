/**
 * Popup actions menu — the legacy "…" row menu (queue rows, playlist rows) and
 * the top-bar gear / add menus. A trigger button toggles a floating panel of
 * menu items; clicking outside or pressing Escape closes it. Items can carry a
 * `tone` (e.g. danger for remove) and a `disabled` flag.
 */

import { useEffect, useRef, useState, type ReactNode } from 'react'
import { cn } from '../lib/cn'

export interface MenuItem {
  label: string
  onClick: () => void
  tone?: 'default' | 'danger'
  disabled?: boolean
}

export function RowMenu({
  trigger,
  items,
  align = 'right',
}: {
  /** The toggle button content (e.g. "…", a gear icon). */
  trigger: ReactNode
  items: MenuItem[]
  align?: 'left' | 'right'
}) {
  const [open, setOpen] = useState(false)
  const wrapRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDoc)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  return (
    <div ref={wrapRef} className="relative inline-flex">
      <button
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={(e) => {
          e.stopPropagation()
          setOpen((o) => !o)
        }}
        className="inline-flex h-8 min-w-8 items-center justify-center rounded-md border border-border bg-bg-sunken px-2 text-sm text-text-dim hover:border-accent hover:text-text"
      >
        {trigger}
      </button>
      {open && (
        <div
          role="menu"
          className={cn(
            'absolute top-full z-40 mt-1 min-w-44 overflow-hidden rounded-md border border-border bg-bg-elev py-1 shadow-[0_8px_24px_rgba(0,0,0,0.4)]',
            align === 'right' ? 'right-0' : 'left-0',
          )}
        >
          {items.map((item, i) => (
            <button
              key={i}
              type="button"
              role="menuitem"
              disabled={item.disabled}
              onClick={(e) => {
                e.stopPropagation()
                setOpen(false)
                item.onClick()
              }}
              className={cn(
                'block w-full px-3 py-2 text-left text-sm hover:bg-bg-sunken disabled:cursor-default disabled:opacity-40',
                item.tone === 'danger' ? 'text-err' : 'text-text',
              )}
            >
              {item.label}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
