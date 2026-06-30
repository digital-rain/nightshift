/**
 * SVG icon set, ported verbatim from the legacy UI (assets/ui/index.html +
 * app.js's *_SVG constants) so stroke widths, paths and the 24×24 viewBox match
 * the bespoke UI exactly. Every icon inherits `currentColor`, so colour comes
 * from the surrounding text class (text-accent, text-text-dim, …).
 *
 * Two families:
 *   - stroke icons (chevrons, nav glyphs, eye, gear, grip, sort, mode glyphs):
 *     fill="none", stroke="currentColor", round caps/joins, weight 2–2.2.
 *   - fill icons (transport: play/pause/skip/stop): fill="currentColor".
 */

import type { SVGProps } from 'react'

type IconProps = SVGProps<SVGSVGElement>

/** Shared wrapper for the stroke-style icons (the legacy 2px round-cap look). */
function Stroke({ children, sw = 2, ...props }: IconProps & { sw?: number }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={sw}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...props}
    >
      {children}
    </svg>
  )
}

/** Shared wrapper for the fill-style transport icons. */
function Fill({ children, ...props }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" {...props}>
      {children}
    </svg>
  )
}

// --- transport (fill) ------------------------------------------------------ //

export const PlayIcon = (p: IconProps) => (
  <Fill {...p}>
    <path d="M7 4v16l13-8z" />
  </Fill>
)

export const PauseIcon = (p: IconProps) => (
  <Fill {...p}>
    <rect x="6" y="4" width="3" height="16" rx="1" />
    <rect x="15" y="4" width="3" height="16" rx="1" />
  </Fill>
)

export const SkipIcon = (p: IconProps) => (
  <Fill {...p}>
    <path d="M6 4v16l11-8z" />
    <rect x="17" y="4" width="3" height="16" rx="1" />
  </Fill>
)

export const StopIcon = (p: IconProps) => (
  <Fill {...p}>
    <rect x="5" y="5" width="14" height="14" rx="1.5" />
  </Fill>
)

// --- chevrons -------------------------------------------------------------- //

/** Right-pointing chevron (disclosure, "›"). */
export const ChevronRightIcon = (p: IconProps) => (
  <Stroke sw={2.2} {...p}>
    <path d="M9 6l6 6-6 6" />
  </Stroke>
)

/** Left-pointing chevron (back, "‹"). */
export const ChevronLeftIcon = (p: IconProps) => (
  <Stroke sw={2.2} {...p}>
    <path d="M15 5l-7 7 7 7" />
  </Stroke>
)

// --- eye / eye-off (playlist hidden toggle) -------------------------------- //

export const EyeIcon = (p: IconProps) => (
  <Stroke {...p}>
    <path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z" />
    <circle cx="12" cy="12" r="3" />
  </Stroke>
)

export const EyeOffIcon = (p: IconProps) => (
  <Stroke {...p}>
    <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94" />
    <path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19" />
    <path d="M9.88 9.88a3 3 0 1 0 4.24 4.24" />
    <line x1="1" y1="1" x2="23" y2="23" />
  </Stroke>
)

// --- chrome: gear, plus, close, grip, sort --------------------------------- //

export const PlusIcon = (p: IconProps) => (
  <Stroke {...p}>
    <path d="M12 5v14M5 12h14" />
  </Stroke>
)

export const CloseIcon = (p: IconProps) => (
  <Stroke sw={2.2} {...p}>
    <path d="M6 6l12 12M18 6L6 18" />
  </Stroke>
)

/** Drag handle / grip (six dots), the legacy queue-row grip. */
export const GripIcon = (p: IconProps) => (
  <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" {...p}>
    <circle cx="9" cy="6" r="1.5" />
    <circle cx="15" cy="6" r="1.5" />
    <circle cx="9" cy="12" r="1.5" />
    <circle cx="15" cy="12" r="1.5" />
    <circle cx="9" cy="18" r="1.5" />
    <circle cx="15" cy="18" r="1.5" />
  </svg>
)

/** Sort-by-priority glyph (lines + up arrow), from the legacy queue-sort button. */
export const SortIcon = (p: IconProps) => (
  <Stroke {...p}>
    <path d="M4 6h11M4 12h7M4 18h3" />
    <path d="M18 8l3-3 3 3M21 5v13" />
  </Stroke>
)

// --- transport modes (segmented control glyphs) ---------------------------- //

/** 1-shot — run a single task once. */
export const ModeOneshotIcon = (p: IconProps) => (
  <Stroke {...p}>
    <circle cx="12" cy="12" r="9" />
    <path d="M10.5 9.5 12.5 8V16" />
    <path d="M10.5 16h4" />
  </Stroke>
)

/** Auto — run the whole queue one time. */
export const ModeAutoIcon = (p: IconProps) => (
  <Stroke {...p}>
    <path d="M17 2l4 4-4 4" />
    <path d="M3 11V9a4 4 0 0 1 4-4h14" />
    <path d="M7 22l-4-4 4-4" />
    <path d="M21 13v2a4 4 0 0 1-4 4H3" />
    <path d="M11 14V10l-1.5 1" />
  </Stroke>
)

/** Repeat — loop the queue continuously. */
export const ModeRepeatIcon = (p: IconProps) => (
  <Stroke {...p}>
    <path d="M17 2l4 4-4 4" />
    <path d="M3 11V9a4 4 0 0 1 4-4h14" />
    <path d="M7 22l-4-4 4-4" />
    <path d="M21 13v2a4 4 0 0 1-4 4H3" />
  </Stroke>
)

// --- bottom-nav glyphs ----------------------------------------------------- //

export const HomeIcon = (p: IconProps) => (
  <Stroke {...p}>
    <path d="M3 11l9-8 9 8" />
    <path d="M5 10v10h14V10" />
  </Stroke>
)

export const NowIcon = (p: IconProps) => (
  <Stroke {...p}>
    <circle cx="12" cy="12" r="9" />
    <path d="M10 9l5 3-5 3z" fill="currentColor" />
  </Stroke>
)

export const QueueIcon = (p: IconProps) => (
  <Stroke {...p}>
    <path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01" />
  </Stroke>
)

export const PlaylistsIcon = (p: IconProps) => (
  <Stroke {...p}>
    <path d="M4 7h11M4 12h11M4 17h7" />
    <circle cx="18" cy="16" r="3" />
    <path d="M21 16V8l-3 1" />
  </Stroke>
)

export const HistoryIcon = (p: IconProps) => (
  <Stroke {...p}>
    <circle cx="12" cy="12" r="9" />
    <path d="M12 7v5l3 2" />
  </Stroke>
)

/** Gear (settings) — drawn with a unicode glyph in the legacy UI; a clean
 *  stroke gear keeps it crisp at any size. */
export const GearIcon = (p: IconProps) => (
  <Stroke {...p}>
    <circle cx="12" cy="12" r="3" />
    <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
  </Stroke>
)
