import React from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import './TeamMemorySummaryCard.css';
import type { TeamMemoryEntry, TeamMemoryUpsertRequest } from '../../api/domains/memory';
import { memoryApi } from '../../api/domains/memory';
import { requestAppConfirm } from '../../components/AppDialogs';

const TEAM_MEMORY_WORKSPACE_KEY = 'workspace';

type TeamMemorySummaryCardProps = {
  agentId: string;
  section: 'aware' | 'workspace';
};

export function buildTeamMemoryQueryKey(workspaceKey: string): string[] {
  return ['team-memory', workspaceKey];
}

export function buildTeamMemoryEntryQueryKey(workspaceKey: string, entryKey: string): string[] {
  return ['team-memory-entry', workspaceKey, entryKey];
}

export function confirmTeamMemoryDelete(
  confirmFn: (message: string) => boolean,
  message: string,
): boolean {
  return confirmFn(message);
}

export function buildTeamMemoryUpsertRequest(
  workspaceKey: string,
  draft: {
    key: string;
    title: string;
    content: string;
    mode: 'replace' | 'append';
  },
  selectedEntry?: Pick<TeamMemoryEntry, 'revision'> | null,
): TeamMemoryUpsertRequest {
  return {
    workspace_key: workspaceKey,
    key: draft.key,
    title: draft.title,
    content: draft.content,
    mode: draft.mode,
    base_revision: selectedEntry?.revision ?? null,
  };
}

export function formatTeamMemoryMutationError(error: unknown, fallback: string): string {
  if (error && typeof error === 'object') {
    const status = 'status' in error ? Number((error as { status?: number }).status) : 0;
    const detail = 'detail' in error ? String((error as { detail?: unknown }).detail ?? '') : '';
    if (status === 409 || detail.toLowerCase().includes('conflict')) {
      return `Team memory conflict: ${detail || fallback}`;
    }
    if (detail) {
      return detail;
    }
  }
  return fallback;
}

function formatUpdatedAt(updatedAt: string): string {
  if (!updatedAt) return '';
  const parsed = new Date(updatedAt);
  if (Number.isNaN(parsed.getTime())) return '';
  return parsed.toLocaleString();
}

export default function TeamMemorySummaryCard({ agentId, section }: TeamMemorySummaryCardProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const isWorkspaceSection = section === 'workspace';
  const [errorMessage, setErrorMessage] = React.useState('');
  const [syncStatus, setSyncStatus] = React.useState<'idle' | 'synced' | 'conflict' | 'error'>('idle');
  const { data: entries = [] } = useQuery({
    queryKey: buildTeamMemoryQueryKey(TEAM_MEMORY_WORKSPACE_KEY),
    queryFn: () => memoryApi.listShared(TEAM_MEMORY_WORKSPACE_KEY),
    enabled: !!agentId,
  });

  const [searchValue, setSearchValue] = React.useState('');
  const [selectedKey, setSelectedKey] = React.useState(entries[0]?.key ?? '');
  const filteredEntries = entries.filter((entry) => {
    const needle = searchValue.trim().toLowerCase();
    if (!needle) return true;
    return `${entry.title}\n${entry.snippet ?? ''}`.toLowerCase().includes(needle);
  });
  const selectedEntryKey = selectedKey || filteredEntries[0]?.key || entries[0]?.key || '';

  const { data: selectedEntry } = useQuery({
    queryKey: buildTeamMemoryEntryQueryKey(TEAM_MEMORY_WORKSPACE_KEY, selectedEntryKey),
    queryFn: () => memoryApi.getShared(selectedEntryKey, TEAM_MEMORY_WORKSPACE_KEY),
    enabled: !!agentId && !!selectedEntryKey,
  });

  const [draftKey, setDraftKey] = React.useState(selectedEntry?.key ?? '');
  const [draftTitle, setDraftTitle] = React.useState(selectedEntry?.title ?? '');
  const [draftContent, setDraftContent] = React.useState(selectedEntry?.content ?? '');
  const [saveMode, setSaveMode] = React.useState<'replace' | 'append'>('replace');

  React.useEffect(() => {
    if (!selectedKey && entries.length > 0) {
      setSelectedKey(entries[0].key);
    }
  }, [entries, selectedKey]);

  React.useEffect(() => {
    if (!isWorkspaceSection || !selectedEntry) {
      return;
    }
    setDraftKey(selectedEntry.key);
    setDraftTitle(selectedEntry.title);
    setDraftContent(selectedEntry.content ?? '');
    setSaveMode('replace');
  }, [isWorkspaceSection, selectedEntry]);

  const saveMutation = useMutation({
    mutationFn: async () =>
      memoryApi.upsertShared(
        buildTeamMemoryUpsertRequest(
          TEAM_MEMORY_WORKSPACE_KEY,
          {
            key: draftKey,
            title: draftTitle,
            content: draftContent,
            mode: saveMode,
          },
          selectedEntry,
        ),
      ),
    onSuccess: (entry) => {
      setErrorMessage('');
      setSyncStatus('synced');
      queryClient.invalidateQueries({ queryKey: buildTeamMemoryQueryKey(TEAM_MEMORY_WORKSPACE_KEY) });
      queryClient.invalidateQueries({ queryKey: buildTeamMemoryEntryQueryKey(TEAM_MEMORY_WORKSPACE_KEY, entry.key) });
      setSelectedKey(entry.key);
    },
    onError: (error) => {
      const message = formatTeamMemoryMutationError(
        error,
        t('agent.workspace.sharedMemorySaveError', 'Failed to save shared memory entry.'),
      );
      setSyncStatus(message.toLowerCase().includes('conflict') ? 'conflict' : 'error');
      setErrorMessage(message);
    },
  });

  const deleteMutation = useMutation({
    mutationFn: async () => memoryApi.deleteShared(selectedEntryKey, TEAM_MEMORY_WORKSPACE_KEY),
    onSuccess: () => {
      setErrorMessage('');
      setSyncStatus('synced');
      queryClient.invalidateQueries({ queryKey: buildTeamMemoryQueryKey(TEAM_MEMORY_WORKSPACE_KEY) });
      if (selectedEntryKey) {
        queryClient.invalidateQueries({ queryKey: buildTeamMemoryEntryQueryKey(TEAM_MEMORY_WORKSPACE_KEY, selectedEntryKey) });
      }
      setSelectedKey('');
      setDraftKey('');
      setDraftTitle('');
      setDraftContent('');
    },
    onError: (error) => {
      setSyncStatus('error');
      setErrorMessage(
        formatTeamMemoryMutationError(
          error,
          t('agent.workspace.sharedMemoryDeleteError', 'Failed to delete shared memory entry.'),
        ),
      );
    },
  });

  const handleDelete = React.useCallback(async () => {
    const confirmed = await requestAppConfirm({
      title: t('common.delete', 'Delete'),
      message: t('agent.workspace.sharedMemoryDeleteConfirm', 'Delete this shared memory entry?'),
      confirmLabel: t('common.delete', 'Delete'),
      danger: true,
    });
    if (!confirmed) {
      return;
    }
    deleteMutation.mutate();
  }, [deleteMutation, t]);

  const titleKey = `agent.${section}.sharedMemoryTitle`;
  const descKey = `agent.${section}.sharedMemoryDesc`;
  const countKey = `agent.${section}.sharedMemoryCount`;
  const updatedKey = `agent.${section}.sharedMemoryUpdated`;
  const searchPlaceholderKey = `agent.${section}.sharedMemorySearchPlaceholder`;
  const emptyKey = `agent.${section}.sharedMemoryEmpty`;
  const detailKey = `agent.${section}.sharedMemoryDetailTitle`;

  return (
    <div className="card team-memory-card">
      <div className="team-memory-head">
        <div>
          <h4 className="team-memory-title">{t(titleKey, 'Shared Team Memory')}</h4>
          <div className="team-memory-desc">
            {t(descKey, 'Recent workspace knowledge reused across sessions.')}
          </div>
        </div>
        <span className="team-memory-count">
          {t(countKey, { count: entries.length, defaultValue: '{{count}} shared entries' })}
        </span>
      </div>

      <input
        value={searchValue}
        onChange={(event) => setSearchValue(event.target.value)}
        placeholder={t(searchPlaceholderKey, 'Search shared memory')}
        className="team-memory-search"
      />

      <div className={`team-memory-grid${isWorkspaceSection ? ' is-workspace' : ''}`}>
        <div className="team-memory-list">
          {filteredEntries.length === 0 ? (
            <div className="team-memory-empty">
              {t(emptyKey, 'No shared memory entries yet.')}
            </div>
          ) : (
            filteredEntries.map((entry) => {
              const isSelected = entry.key === selectedEntryKey;
              return (
                <button
                  key={`${entry.workspace_key}:${entry.key}`}
                  type="button"
                  onClick={() => setSelectedKey(entry.key)}
                  className={`team-memory-entry${isSelected ? ' is-selected' : ''}`}
                >
                  <div className="team-memory-entry-title">{entry.title}</div>
                  {entry.snippet && (
                    <div className="team-memory-entry-snippet">
                      {entry.snippet}
                    </div>
                  )}
                </button>
              );
            })
          )}
        </div>

        <div className="team-memory-detail-col">
          <div className="team-memory-panel team-memory-detail">
            <div className="team-memory-detail-head">
              <div>
                <div className="team-memory-detail-label">
                  {t(detailKey, 'Entry Details')}
                </div>
                <div className="team-memory-detail-title">{selectedEntry?.title ?? filteredEntries[0]?.title ?? t(emptyKey, 'No shared memory entries yet.')}</div>
              </div>
              {selectedEntry?.updated_at && (
                <span className="team-memory-detail-updated">
                  {t(updatedKey, { time: formatUpdatedAt(selectedEntry.updated_at), defaultValue: 'Updated {{time}}' })}
                </span>
              )}
            </div>
            <div className="team-memory-detail-content">
              {selectedEntry?.content ?? selectedEntry?.snippet ?? filteredEntries[0]?.snippet ?? t(emptyKey, 'No shared memory entries yet.')}
            </div>
            {selectedEntry && (
              <div className="team-memory-detail-rev">
                {`rev ${selectedEntry.revision ?? 0}${selectedEntry.updated_by ? ` · ${selectedEntry.updated_by}` : ''}`}
              </div>
            )}
          </div>

          {isWorkspaceSection && (
            <div className="team-memory-panel team-memory-editor">
              <input
                value={draftKey}
                onChange={(event) => setDraftKey(event.target.value)}
                placeholder={t('agent.workspace.sharedMemoryKeyPlaceholder', 'Entry key')}
                className="team-memory-field"
              />
              <input
                value={draftTitle}
                onChange={(event) => setDraftTitle(event.target.value)}
                placeholder={t('agent.workspace.sharedMemoryTitlePlaceholder', 'Entry title')}
                className="team-memory-field"
              />
              <textarea
                value={draftContent}
                onChange={(event) => setDraftContent(event.target.value)}
                placeholder={t('agent.workspace.sharedMemoryContentPlaceholder', 'Write the shared note or playbook here')}
                rows={6}
                className="team-memory-textarea"
              />
              <div className="team-memory-mode-row">
                <select
                  value={saveMode}
                  onChange={(event) => setSaveMode(event.target.value as 'replace' | 'append')}
                  className="team-memory-select"
                >
                  <option value="replace">{t('agent.workspace.sharedMemoryModeReplace', 'Replace')}</option>
                  <option value="append">{t('agent.workspace.sharedMemoryModeAppend', 'Append')}</option>
                </select>
                <button type="button" className="btn btn-primary" onClick={() => saveMutation.mutate()} disabled={saveMutation.isPending || !draftKey || !draftTitle || !draftContent}>
                  {t('agent.workspace.sharedMemorySave', 'Save to shared memory')}
                </button>
                <button type="button" className="btn btn-ghost" onClick={handleDelete} disabled={deleteMutation.isPending || !selectedEntryKey}>
                  {t('agent.workspace.sharedMemoryDelete', 'Delete entry')}
                </button>
              </div>
              {errorMessage && (
                <div className="team-memory-error">
                  {errorMessage}
                </div>
              )}
              <div className="team-memory-sync">
                {syncStatus === 'synced'
                  ? t('agent.workspace.sharedMemorySyncSynced', 'Shared memory synced')
                  : syncStatus === 'conflict'
                    ? t('agent.workspace.sharedMemorySyncConflict', 'Shared memory conflict')
                    : syncStatus === 'error'
                      ? t('agent.workspace.sharedMemorySyncError', 'Shared memory error')
                      : t('agent.workspace.sharedMemorySyncIdle', 'Shared memory idle')}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
