/**
 * Spin-ring — the small accent loading ring shown on the now-playing queue row,
 * ported from the legacy `.q-spin` / `spin-ring` (border-top transparent, 0.7s
 * linear). Rows reserve its 14px slot even when idle so there's no layout shift
 * when a task starts running.
 */

import { cn } from '../lib/cn'

export function SpinRing({
  active,
  className,
}: {
  active?: boolean
  className?: string
}) {
  return (
    <span
      className={cn('inline-flex h-3.5 w-3.5 shrink-0 items-center justify-center', className)}
      aria-hidden={!active}
    >
      {active && (
        <span className="h-3.5 w-3.5 animate-spin rounded-full border-[1.5px] border-accent border-t-transparent" />
      )}
    </span>
  )
}
