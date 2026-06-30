/**
 * Shared settings-save hook for both the manager and worker surfaces.
 *
 * Owns the three things the two SettingsScreen/SettingsView call sites used to
 * duplicate (and got wrong): the `saving` flag, error surfacing (the old inline
 * onSave swallowed failures — a rejected save looked like success), and the
 * shaping of the editor's flat `surface/category/key` delta into the nested
 * surface→category→field body the backend's PUT /api/settings expects.
 *
 * Pass the surface-appropriate saver (manager.saveSettings or
 * workerUi.saveSettings) and a refetch; get back { save, saving, error }.
 */

import { useState } from 'react'
import type { SettingsDelta } from '../components/SettingsEditor'
import type { SettingsSaveResponse } from '../api/types'

/** Turn the flat path-keyed delta into the nested body the backend expects.
 * Path is `surface/category/key` (see SettingsEditor.fieldPath). Slashes only
 * ever come from those three segments, so a 3-way split is exact. */
export function shapeSettingsDelta(
  delta: SettingsDelta,
): Record<string, Record<string, Record<string, unknown>>> {
  const body: Record<string, Record<string, Record<string, unknown>>> = {}
  for (const [path, value] of Object.entries(delta)) {
    const slash1 = path.indexOf('/')
    const slash2 = path.indexOf('/', slash1 + 1)
    if (slash1 < 0 || slash2 < 0) continue // malformed; skip rather than mis-nest
    const surface = path.slice(0, slash1)
    const category = path.slice(slash1 + 1, slash2)
    const key = path.slice(slash2 + 1)
    ;(body[surface] ??= {})[category] ??= {}
    body[surface][category][key] = value
  }
  return body
}

export interface UseSettingsSave {
  save: (delta: SettingsDelta) => Promise<void>
  saving: boolean
  error: Error | null
}

export function useSettingsSave(
  saver: (body: unknown) => Promise<SettingsSaveResponse>,
  refetch: () => Promise<unknown>,
): UseSettingsSave {
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<Error | null>(null)

  async function save(delta: SettingsDelta) {
    setSaving(true)
    setError(null)
    try {
      const res = await saver(shapeSettingsDelta(delta))
      // The backend reports per-field validation failures with ok:false.
      if (res && res.ok === false) {
        throw new Error(
          res.errors?.join('; ') || 'Settings save was rejected.',
        )
      }
      await refetch()
    } catch (e) {
      setError(e instanceof Error ? e : new Error(String(e)))
    } finally {
      setSaving(false)
    }
  }

  return { save, saving, error }
}
