/**
 * Dark/light theme toggle, preserving the legacy contract: a `data-theme`
 * attribute on <html> ("dark" default, "light" opt-in). theme.css re-points the
 * design tokens under :root[data-theme="light"], so flipping the attribute
 * recolours the whole UI with no React re-render of consumers. The choice is
 * persisted to localStorage under the legacy key.
 */

import { useCallback, useEffect, useState } from 'react'

export type Theme = 'dark' | 'light'

const KEY = 'nightshift-theme'

function read(): Theme {
  const attr = document.documentElement.getAttribute('data-theme')
  if (attr === 'light' || attr === 'dark') return attr
  const stored = localStorage.getItem(KEY)
  return stored === 'light' ? 'light' : 'dark'
}

export function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(read)

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
    localStorage.setItem(KEY, theme)
  }, [theme])

  const toggle = useCallback(
    () => setTheme((t) => (t === 'dark' ? 'light' : 'dark')),
    [],
  )

  return [theme, toggle]
}
