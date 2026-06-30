/**
 * Modal dialog shell — ported from the legacy `.modal` / `.modal-card`. A dimmed
 * full-screen overlay centring a bordered, shadowed card with a head (title +
 * close ✕) and an optional `.modal-actions` footer. Closes on ✕, backdrop click,
 * or Escape. Shared by the new-task, new-queue, and add-from/add-to dialogs.
 */

import { useEffect, type ReactNode } from 'react'
import { IconButton } from './primitives'
import { CloseIcon } from './icons'

export function Modal({
  title,
  onClose,
  actions,
  children,
}: {
  title: ReactNode
  onClose: () => void
  /** Footer buttons (rendered in `.modal-actions`, right-aligned). */
  actions?: ReactNode
  children: ReactNode
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      onClick={onClose}
      role="presentation"
    >
      <div
        className="flex max-h-[85vh] w-full max-w-lg flex-col overflow-hidden rounded-[14px] border border-border bg-bg-elev shadow-[0_8px_24px_rgba(0,0,0,0.4)]"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
      >
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <h3 className="text-base font-semibold text-text">{title}</h3>
          <IconButton title="Close" aria-label="Close" onClick={onClose}>
            <CloseIcon className="h-4 w-4" />
          </IconButton>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4">{children}</div>
        {actions && (
          <div className="flex justify-end gap-2 border-t border-border px-4 py-3">
            {actions}
          </div>
        )}
      </div>
    </div>
  )
}
