/**
 * Small formatting helpers shared across run rows, stats tables, and detail
 * panes. Pure functions, no React — keep display logic out of components.
 */

export function fmtCost(usd: number | null | undefined): string {
  if (usd == null) return '—'
  return `$${usd.toFixed(usd < 1 ? 3 : 2)}`
}

export function fmtInt(n: number | null | undefined): string {
  return n == null ? '—' : n.toLocaleString()
}

export function fmtTokens(n: number | null | undefined): string {
  if (n == null) return '—'
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`
  return String(n)
}

/** Elapsed seconds between two ISO timestamps, or null if not finishable. */
export function elapsedSeconds(
  startedAt: string | null | undefined,
  finishedAt: string | null | undefined,
): number | null {
  if (!startedAt) return null
  const start = Date.parse(startedAt)
  const end = finishedAt ? Date.parse(finishedAt) : Date.now()
  if (Number.isNaN(start) || Number.isNaN(end)) return null
  return Math.max(0, (end - start) / 1000)
}

export function fmtDuration(seconds: number | null | undefined): string {
  if (seconds == null) return '—'
  if (seconds < 60) return `${seconds.toFixed(0)}s`
  const m = Math.floor(seconds / 60)
  const s = Math.round(seconds % 60)
  if (m < 60) return `${m}m ${s}s`
  const h = Math.floor(m / 60)
  return `${h}h ${m % 60}m`
}

/** Relative "3m ago" style timestamp for history rows. */
export function fmtAgo(iso: string | null | undefined): string {
  if (!iso) return '—'
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return '—'
  const sec = Math.max(0, (Date.now() - t) / 1000)
  if (sec < 60) return `${Math.floor(sec)}s ago`
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`
  return `${Math.floor(sec / 86400)}d ago`
}

/** Queue label normalisation — the backend treats null / "" as the main queue. */
export function queueLabel(queue: string | null | undefined): string {
  return queue && queue !== 'main' ? queue : 'main'
}
