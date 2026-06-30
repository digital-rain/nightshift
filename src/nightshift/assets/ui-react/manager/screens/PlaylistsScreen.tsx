/**
 * Playlists screen — the legacy #screen-playlists. Lists every queue/playlist
 * with its task-count badge, a hide/unhide (eye) toggle, an info → playlist-info
 * takeover, and a per-row menu. Chrome: a Show-hidden toggle, a Rescan button
 * (discover workspace repos → playlists), and + New (the new-queue modal).
 */

import { useState } from 'react'
import type { Playlist } from '../../src/api/types'
import { Modal } from '../../src/components/Modal'
import { RowMenu } from '../../src/components/RowMenu'
import { TextField } from '../../src/components/fields'
import {
  Count,
  EmptyState,
  ErrorState,
  GhostButton,
  IconButton,
  PrimaryButton,
  Spinner,
} from '../../src/components/primitives'
import { EyeIcon, EyeOffIcon } from '../../src/components/icons'
import { DetailTakeover } from '../../src/components/DetailTakeover'
import { cn } from '../../src/lib/cn'
import {
  useCreatePlaylist,
  useDeletePlaylist,
  usePlaylists,
  useRescanPlaylists,
  useUpdatePlaylist,
} from '../../src/hooks/managerQueries'

export function PlaylistsScreen() {
  const { data, isLoading, error } = usePlaylists()
  const rescan = useRescanPlaylists()
  const update = useUpdatePlaylist()
  const del = useDeletePlaylist()
  const [showHidden, setShowHidden] = useState(false)
  const [creating, setCreating] = useState(false)
  const [editing, setEditing] = useState<Playlist | null>(null)

  if (error) return <ErrorState error={error} />

  const playlists = (data ?? []).filter((p) => showHidden || !p.disabled)

  if (editing) {
    return <PlaylistInfo playlist={editing} onBack={() => setEditing(null)} />
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
        <div className="flex items-center">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-text-dim">
            Playlists
          </h2>
          <Count value={playlists.length} />
        </div>
        <div className="flex items-center gap-2">
          <GhostButton
            aria-pressed={showHidden}
            onClick={() => setShowHidden((s) => !s)}
            title="Show hidden (disabled) playlists"
          >
            Hidden {showHidden ? '✓' : ''}
          </GhostButton>
          <GhostButton onClick={() => rescan.mutate()} title="Scan the workspace for git repos">
            Rescan
          </GhostButton>
          <PrimaryButton onClick={() => setCreating(true)}>+ New</PrimaryButton>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {isLoading ? (
          <Spinner />
        ) : playlists.length === 0 ? (
          <EmptyState>No playlists yet.</EmptyState>
        ) : (
          <ul className="list-none">
            {playlists.map((p) => (
              <li
                key={p.name}
                className={cn(
                  'flex items-center gap-3 border-b border-border px-4 py-3',
                  p.disabled && 'opacity-55',
                )}
              >
                <span className={cn('font-medium text-text', p.name === 'library' && 'italic')}>
                  {p.name}
                </span>
                <Count value={p.task_count} />
                {p.name === 'library' && (
                  <span className="text-[11px] uppercase tracking-wide text-text-dim">
                    library
                  </span>
                )}
                <div className="ml-auto flex items-center gap-2">
                  <IconButton
                    title={p.disabled ? 'Unhide' : 'Hide'}
                    aria-label={p.disabled ? 'Unhide' : 'Hide'}
                    onClick={() =>
                      update.mutate({ name: p.name, body: { disabled: !p.disabled } })
                    }
                  >
                    {p.disabled ? <EyeOffIcon className="h-4 w-4" /> : <EyeIcon className="h-4 w-4" />}
                  </IconButton>
                  <RowMenu
                    trigger="⋯"
                    items={[
                      { label: 'Info / repository…', onClick: () => setEditing(p) },
                      {
                        label: 'Delete',
                        tone: 'danger',
                        disabled: p.name === 'library',
                        onClick: () => del.mutate(p.name),
                      },
                    ]}
                  />
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>

      {creating && <NewQueueModal onClose={() => setCreating(false)} />}
    </div>
  )
}

/** Playlist-info takeover — edit the playlist's name + repository. */
function PlaylistInfo({ playlist, onBack }: { playlist: Playlist; onBack: () => void }) {
  const update = useUpdatePlaylist()
  const [name, setName] = useState(playlist.name)
  const [repo, setRepo] = useState(playlist.repository ?? '')
  return (
    <DetailTakeover
      title="Playlist info"
      onBack={onBack}
      backTitle="Cancel"
      footer={
        <div className="flex justify-end gap-2">
          <GhostButton onClick={onBack}>Cancel</GhostButton>
          <PrimaryButton
            disabled={update.isPending}
            onClick={() =>
              update.mutate(
                {
                  name: playlist.name,
                  body: { name, repository: repo || null },
                },
                { onSuccess: onBack },
              )
            }
          >
            {update.isPending ? 'Saving…' : 'Save'}
          </PrimaryButton>
        </div>
      }
    >
      <TextField label="Name" value={name} onChange={setName} />
      <TextField
        label="Repository"
        desc="Workspace-relative repo this queue's tasks run against."
        value={repo}
        onChange={setRepo}
        placeholder="— inherit / none —"
      />
    </DetailTakeover>
  )
}

/** New-queue modal — create a self-contained playlist. */
function NewQueueModal({ onClose }: { onClose: () => void }) {
  const create = useCreatePlaylist()
  const [name, setName] = useState('')
  return (
    <Modal
      title="New queue"
      onClose={onClose}
      actions={
        <>
          <GhostButton onClick={onClose}>Cancel</GhostButton>
          <PrimaryButton
            disabled={!name.trim() || create.isPending}
            onClick={() => create.mutate(name.trim(), { onSuccess: onClose })}
          >
            {create.isPending ? 'Creating…' : 'Create'}
          </PrimaryButton>
        </>
      }
    >
      <TextField
        label="Name"
        desc="A self-contained playlist under .tasks/<name> that inherits the main queue's settings."
        value={name}
        onChange={setName}
        placeholder="e.g. nightshift"
      />
      {create.error != null && (
        <p className="text-sm text-err">{String(create.error)}</p>
      )}
    </Modal>
  )
}
