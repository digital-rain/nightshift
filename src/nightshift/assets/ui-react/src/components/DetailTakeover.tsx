/**
 * DetailTakeover — the full-area takeover shell shared by the task-detail,
 * playlist-info, and stats screens in the legacy UI (.detail-takeover-head + a
 * scrolling body, with a back chevron whose semantics are "cancel / discard").
 *
 * It owns only the chrome (back chevron + title + optional header actions + a
 * scroll container). Callers fill the body and decide what `onBack` discards.
 */

import type { ReactNode } from 'react'

function ChevronLeft() {
  return (
    <svg
      viewBox="0 0 24 24"
      width="20"
      height="20"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M15 5l-7 7 7 7" />
    </svg>
  )
}

export function DetailTakeover({
  title,
  onBack,
  backTitle = 'Back',
  actions,
  footer,
  children,
}: {
  title: ReactNode
  onBack: () => void
  backTitle?: string
  /** Header-right controls. */
  actions?: ReactNode
  /** Sticky footer (e.g. a save bar). */
  footer?: ReactNode
  children: ReactNode
}) {
  return (
    <section className="flex min-h-0 flex-1 flex-col">
      <div className="flex items-center gap-3 border-b border-border px-3 py-3">
        <button
          type="button"
          onClick={onBack}
          title={backTitle}
          aria-label={backTitle}
          className="inline-flex h-8 w-8 items-center justify-center rounded-md text-text-dim hover:bg-bg-elev hover:text-text"
        >
          <ChevronLeft />
        </button>
        <h2 className="flex-1 truncate text-base font-semibold">{title}</h2>
        {actions != null && (
          <div className="flex items-center gap-2">{actions}</div>
        )}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4">{children}</div>

      {footer != null && (
        <div className="border-t border-border bg-bg-elev px-4 py-3">
          {footer}
        </div>
      )}
    </section>
  )
}
