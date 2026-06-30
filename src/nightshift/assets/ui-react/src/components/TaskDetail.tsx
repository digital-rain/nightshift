/**
 * TaskDetail — read / edit a single task inside a DetailTakeover.
 *
 * Generalises the legacy task-detail and new-task draft surfaces: given a
 * TaskDetail payload (or the task-defaults payload, where `task` is null) it
 * renders the title + body editors plus the frontmatter controls (model,
 * priority, draft/automerge/evergreen flags). The back chevron is "cancel":
 * unsaved edits are discarded, matching the legacy "back is cancel" contract.
 *
 * It is presentational + local-edit-state only; the parent supplies onSave /
 * onBack so the same component works against the manager and (read-only) worker
 * surfaces.
 *
 * IMPORTANT: the edit fields are seeded from props via useState, which only
 * reads its initializer on mount. To switch to a different task, the caller
 * MUST remount this component — render it with `key={task}` (the manager queue
 * does). Without a key, swapping `detail` in place would keep the previous
 * task's field values and a Save would write them onto the new task.
 */

import { useState } from 'react'
import type { TaskDetail as TaskDetailModel, TaskUpdate } from '../api/types'
import { DetailTakeover } from './DetailTakeover'
import { PrimaryButton, GhostButton } from './primitives'
import {
  CheckboxField,
  NumberField,
  SelectField,
  TextAreaField,
  TextField,
} from './fields'

export interface TaskDetailProps {
  detail: TaskDetailModel
  onBack: () => void
  /** Persist the edited fields. Omit for a read-only view (worker surface). */
  onSave?: (patch: TaskUpdate) => void
  saving?: boolean
  readOnly?: boolean
}

export function TaskDetail({
  detail,
  onBack,
  onSave,
  saving,
  readOnly,
}: TaskDetailProps) {
  const fm = detail.frontmatter
  const [title, setTitle] = useState(detail.title)
  const [body, setBody] = useState(detail.body)
  const [model, setModel] = useState(fm.model ?? '')
  const [priority, setPriority] = useState<number | ''>(fm.priority)
  const [draft, setDraft] = useState(fm.draft)
  const [automerge, setAutomerge] = useState(fm.automerge)
  const [evergreen, setEvergreen] = useState(detail.evergreen)

  const isNew = detail.task == null
  const editable = !readOnly

  const modelOptions = [
    { value: '', label: '— default —' },
    ...detail.model_options.map((m) => ({ value: m, label: m })),
  ]

  function handleSave() {
    if (!onSave) return
    const patch: TaskUpdate = {
      title,
      body,
      model: model || null,
      priority: priority === '' ? undefined : priority,
      draft,
      automerge,
      evergreen,
    }
    onSave(patch)
  }

  const footer = editable ? (
    <div className="flex items-center justify-end gap-2">
      <GhostButton onClick={onBack}>Cancel</GhostButton>
      <PrimaryButton onClick={handleSave} disabled={saving}>
        {saving ? 'Saving…' : isNew ? 'Create' : 'Save'}
      </PrimaryButton>
    </div>
  ) : undefined

  return (
    <DetailTakeover
      title={isNew ? 'New task' : detail.task!}
      onBack={onBack}
      backTitle="Back (discards edits)"
      footer={footer}
    >
      <TextField
        id="task-title"
        label="Title"
        value={title}
        onChange={editable ? setTitle : () => {}}
      />
      <TextAreaField
        id="task-body"
        label="Body"
        desc="The task prompt handed to the agent."
        value={body}
        onChange={editable ? setBody : () => {}}
        rows={14}
      />

      <div className="grid grid-cols-1 gap-x-6 sm:grid-cols-2">
        <SelectField
          id="task-model"
          label="Model"
          value={model}
          onChange={editable ? setModel : () => {}}
          options={modelOptions}
        />
        <NumberField
          id="task-priority"
          label="Priority"
          desc="0 (highest) … 5 (lowest)."
          value={priority}
          onChange={editable ? setPriority : () => {}}
        />
      </div>

      <CheckboxField
        id="task-draft"
        label="Draft"
        desc="Open the resulting PR as a draft."
        checked={draft}
        onChange={editable ? setDraft : () => {}}
      />
      <CheckboxField
        id="task-automerge"
        label="Auto-merge"
        desc="Merge automatically once checks pass."
        checked={automerge}
        onChange={editable ? setAutomerge : () => {}}
      />
      <CheckboxField
        id="task-evergreen"
        label="Evergreen"
        desc="Re-runnable; not marked complete after a successful run."
        checked={evergreen}
        onChange={editable ? setEvergreen : () => {}}
      />
    </DetailTakeover>
  )
}
