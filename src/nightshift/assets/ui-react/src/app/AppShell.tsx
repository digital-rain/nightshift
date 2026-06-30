/**
 * AppShell — the top bar + scrolling content frame shared by both surfaces.
 * Ports the legacy .topbar (brand + actions) and the body region. The bottom
 * tab nav differs per surface, so it is passed in as `nav`.
 */

import type { ReactNode } from 'react'

export function AppShell({
  brandName,
  brandTag,
  logoSrc,
  actions,
  nav,
  children,
}: {
  brandName: string
  brandTag?: ReactNode
  logoSrc?: string
  actions?: ReactNode
  nav?: ReactNode
  children: ReactNode
}) {
  return (
    <>
      <header className="flex items-center justify-between gap-4 border-b border-border bg-bg-elev px-4 py-2.5 shadow-[var(--shadow-ns)]">
        <div className="flex min-w-0 items-center gap-2.5">
          {logoSrc && (
            <img src={logoSrc} alt={brandName} className="block h-8 w-auto" />
          )}
          <div className="flex min-w-0 flex-col leading-tight">
            <span className="text-[15px] font-bold text-text">{brandName}</span>
            {brandTag != null && (
              <span className="truncate text-[10px] uppercase tracking-wider text-text-dim">
                {brandTag}
              </span>
            )}
          </div>
        </div>
        {actions != null && (
          <div className="flex items-center gap-2">{actions}</div>
        )}
      </header>

      <main className="flex min-h-0 flex-1 flex-col">{children}</main>

      {nav != null && (
        <nav className="flex items-stretch border-t border-border bg-bg-elev">
          {nav}
        </nav>
      )}
    </>
  )
}

/** A single bottom-nav tab button (.navbtn / .nav-opt). */
export function NavTab({
  active,
  onClick,
  icon,
  label,
}: {
  active: boolean
  onClick: () => void
  icon?: ReactNode
  label: string
}) {
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      onClick={onClick}
      className={`flex flex-1 flex-col items-center gap-0.5 py-2 text-[11px] ${
        active ? 'text-accent' : 'text-text-dim hover:text-text'
      }`}
    >
      {icon}
      <span>{label}</span>
    </button>
  )
}
