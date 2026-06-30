/**
 * SettingsEditor — the multi-tier settings surface shared by the manager and
 * worker UIs. The backend returns the SAME tier/category/field shape from both
 * /api/settings endpoints (the worker just exposes fewer surfaces), so one
 * editor drives both: a left tier+category tree, a right field pane, and a
 * dirty-tracking save bar that PUTs a nested delta.
 *
 * Edits are tracked as a flat {fieldKey: newValue} delta against each field's
 * `effective` value; the parent's onSave turns that into the nested
 * surface→category→field body the backend expects.
 */

import { useMemo, useState } from 'react'
import type {
  SettingsField,
  SettingsResponse,
  SettingsTier,
} from '../api/types'
import {
  CheckboxField,
  NumberField,
  SelectField,
  TextField,
} from './fields'
import { GhostButton, Pill, PrimaryButton } from './primitives'

/** Map of fieldKey → edited value (the working delta). */
export type SettingsDelta = Record<string, unknown>

export interface SettingsEditorProps {
  data: SettingsResponse
  saving?: boolean
  /** Receives the working delta keyed by field key; map to backend shape in the parent. */
  onSave: (delta: SettingsDelta) => void
}

function fieldValue(f: SettingsField, delta: SettingsDelta): unknown {
  return f.key in delta ? delta[f.key] : f.effective
}

function FieldControl({
  field,
  delta,
  setValue,
}: {
  field: SettingsField
  delta: SettingsDelta
  setValue: (key: string, value: unknown) => void
}) {
  const value = fieldValue(field, delta)
  const desc = (
    <>
      {field.desc}
      {field.apply === 'restart' && (
        <Pill tone="warn" className="ml-2">
          restart
        </Pill>
      )}
      {field.env_shadowed && (
        <Pill tone="neutral" className="ml-2" title={field.env ?? undefined}>
          env
        </Pill>
      )}
    </>
  )

  const disabled = field.type === 'readonly' || field.env_shadowed
  if (disabled) {
    return (
      <TextField
        id={field.key}
        label={field.label}
        desc={desc}
        value={String(value ?? '')}
        onChange={() => {}}
      />
    )
  }

  switch (field.type) {
    case 'bool':
      return (
        <CheckboxField
          id={field.key}
          label={field.label}
          desc={desc}
          checked={Boolean(value)}
          onChange={(v) => setValue(field.key, v)}
        />
      )
    case 'int':
    case 'duration':
      return (
        <NumberField
          id={field.key}
          label={field.label}
          desc={desc}
          value={value === null || value === undefined ? '' : Number(value)}
          onChange={(v) => setValue(field.key, v === '' ? null : v)}
        />
      )
    case 'enum':
      return (
        <SelectField
          id={field.key}
          label={field.label}
          desc={desc}
          value={String(value ?? '')}
          onChange={(v) => setValue(field.key, v)}
          options={(field.options ?? []).map((o) => ({ value: o, label: o }))}
        />
      )
    case 'str_list':
    case 'int_list':
    case 'str_map':
      // Edited as text; the parent/backend parse. Kept simple here on purpose.
      return (
        <TextField
          id={field.key}
          label={field.label}
          desc={desc}
          value={
            Array.isArray(value)
              ? (value as unknown[]).join(', ')
              : String(value ?? '')
          }
          onChange={(v) => setValue(field.key, v)}
        />
      )
    default:
      return (
        <TextField
          id={field.key}
          label={field.label}
          desc={desc}
          value={field.secret ? '' : String(value ?? '')}
          onChange={(v) => setValue(field.key, v)}
        />
      )
  }
}

export function SettingsEditor({ data, saving, onSave }: SettingsEditorProps) {
  const tiers = data.tiers
  const [tierIdx, setTierIdx] = useState(0)
  const [catIdx, setCatIdx] = useState(0)
  const [search, setSearch] = useState('')
  const [delta, setDelta] = useState<SettingsDelta>({})

  const activeTier: SettingsTier | undefined = tiers[tierIdx]
  const activeCat = activeTier?.categories[catIdx]

  const filteredFields = useMemo(() => {
    const fields = activeCat?.fields ?? []
    if (!search.trim()) return fields
    const q = search.toLowerCase()
    return fields.filter(
      (f) =>
        f.label.toLowerCase().includes(q) ||
        f.key.toLowerCase().includes(q) ||
        f.desc.toLowerCase().includes(q),
    )
  }, [activeCat, search])

  const dirtyCount = Object.keys(delta).length

  function setValue(key: string, value: unknown) {
    setDelta((d) => ({ ...d, [key]: value }))
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex min-h-0 flex-1">
        {/* sidebar: tier selector + category tree */}
        <aside className="w-56 shrink-0 overflow-y-auto border-r border-border">
          <div className="p-2">
            <input
              type="search"
              placeholder="Search settings…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="w-full rounded-md border border-border bg-bg-sunken px-2 py-1.5 text-sm text-text outline-none focus:border-accent"
            />
          </div>
          <nav className="px-2 pb-2">
            {tiers.map((tier, ti) => (
              <div key={tier.surface} className="mb-3">
                <button
                  type="button"
                  onClick={() => {
                    setTierIdx(ti)
                    setCatIdx(0)
                  }}
                  className={`mb-1 block w-full text-left text-xs font-semibold uppercase tracking-wide ${
                    ti === tierIdx ? 'text-accent' : 'text-text-dim'
                  }`}
                >
                  {tier.surface}
                </button>
                {ti === tierIdx &&
                  tier.categories.map((cat, ci) => (
                    <button
                      key={cat.name}
                      type="button"
                      onClick={() => setCatIdx(ci)}
                      className={`block w-full rounded px-2 py-1 text-left text-sm ${
                        ci === catIdx
                          ? 'bg-bg-elev text-text'
                          : 'text-text-dim hover:text-text'
                      }`}
                    >
                      {cat.name}
                    </button>
                  ))}
              </div>
            ))}
          </nav>
        </aside>

        {/* field pane */}
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          {filteredFields.map((f) => (
            <FieldControl
              key={f.key}
              field={f}
              delta={delta}
              setValue={setValue}
            />
          ))}
          {filteredFields.length === 0 && (
            <p className="py-8 text-center text-sm text-text-dim">
              No matching settings.
            </p>
          )}
        </div>
      </div>

      {/* save bar */}
      {dirtyCount > 0 && (
        <div className="flex items-center justify-between border-t border-border bg-bg-elev px-4 py-3">
          <span className="text-sm text-text-dim">
            {dirtyCount} unsaved {dirtyCount === 1 ? 'change' : 'changes'}
          </span>
          <div className="flex items-center gap-2">
            <GhostButton onClick={() => setDelta({})}>Discard</GhostButton>
            <PrimaryButton onClick={() => onSave(delta)} disabled={saving}>
              {saving ? 'Saving…' : 'Save'}
            </PrimaryButton>
          </div>
        </div>
      )}
    </div>
  )
}
