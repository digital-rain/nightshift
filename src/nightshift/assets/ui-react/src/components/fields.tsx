/**
 * Form field primitives shared by TaskDetail and SettingsEditor — labelled
 * text / textarea / select / checkbox controls with a description line, ported
 * from the legacy <label> + .field-desc pattern. Controlled components only.
 */

import type { ReactNode } from 'react'

function FieldShell({
  label,
  desc,
  htmlFor,
  children,
}: {
  label: ReactNode
  desc?: ReactNode
  htmlFor?: string
  children: ReactNode
}) {
  return (
    <div className="mb-4">
      <label
        htmlFor={htmlFor}
        className="mb-1 block text-sm font-medium text-text"
      >
        {label}
      </label>
      {children}
      {desc != null && (
        <p className="mt-1 text-xs text-text-dim">{desc}</p>
      )}
    </div>
  )
}

const inputClass =
  'w-full rounded-md border border-border bg-bg-sunken px-3 py-2 text-sm text-text outline-none focus:border-accent'

export function TextField({
  label,
  desc,
  value,
  onChange,
  placeholder,
  id,
}: {
  label: ReactNode
  desc?: ReactNode
  value: string
  onChange: (v: string) => void
  placeholder?: string
  id?: string
}) {
  return (
    <FieldShell label={label} desc={desc} htmlFor={id}>
      <input
        id={id}
        type="text"
        className={inputClass}
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
      />
    </FieldShell>
  )
}

export function NumberField({
  label,
  desc,
  value,
  onChange,
  id,
}: {
  label: ReactNode
  desc?: ReactNode
  value: number | ''
  onChange: (v: number | '') => void
  id?: string
}) {
  return (
    <FieldShell label={label} desc={desc} htmlFor={id}>
      <input
        id={id}
        type="number"
        className={inputClass}
        value={value}
        onChange={(e) =>
          onChange(e.target.value === '' ? '' : Number(e.target.value))
        }
      />
    </FieldShell>
  )
}

export function TextAreaField({
  label,
  desc,
  value,
  onChange,
  rows = 8,
  id,
}: {
  label: ReactNode
  desc?: ReactNode
  value: string
  onChange: (v: string) => void
  rows?: number
  id?: string
}) {
  return (
    <FieldShell label={label} desc={desc} htmlFor={id}>
      <textarea
        id={id}
        rows={rows}
        className={`${inputClass} font-mono`}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    </FieldShell>
  )
}

export function SelectField({
  label,
  desc,
  value,
  onChange,
  options,
  id,
}: {
  label: ReactNode
  desc?: ReactNode
  value: string
  onChange: (v: string) => void
  options: Array<{ value: string; label: string }>
  id?: string
}) {
  return (
    <FieldShell label={label} desc={desc} htmlFor={id}>
      <select
        id={id}
        className={inputClass}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </FieldShell>
  )
}

export function CheckboxField({
  label,
  desc,
  checked,
  onChange,
  id,
}: {
  label: ReactNode
  desc?: ReactNode
  checked: boolean
  onChange: (v: boolean) => void
  id?: string
}) {
  return (
    <div className="mb-4">
      <label htmlFor={id} className="flex cursor-pointer items-center gap-2">
        <input
          id={id}
          type="checkbox"
          className="h-4 w-4 accent-[var(--color-accent)]"
          checked={checked}
          onChange={(e) => onChange(e.target.checked)}
        />
        <span className="text-sm font-medium text-text">{label}</span>
      </label>
      {desc != null && (
        <p className="mt-1 ml-6 text-xs text-text-dim">{desc}</p>
      )}
    </div>
  )
}
