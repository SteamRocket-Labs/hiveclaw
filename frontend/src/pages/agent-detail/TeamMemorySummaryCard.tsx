import React from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import './TeamMemorySummaryCard.css';
import type { TeamMemoryEntry, TeamMemoryUpsertRequest, TeamMemoryWorkspace } from '../../api/domains/memory';
import { memoryApi } from '../../api/domains/memory';
import { requestAppConfirm } from '../../components/AppDialogs';
import { useAuthStore } from '../../stores';
import { useAssistantTenant } from '../assistant/assistantScope';
import { ApiError } from '../../api/core/errors';

type TeamMemorySummaryCardProps = {
  agentId: string;
  section: 'aware' | 'workspace';
};

type MemoryDraft = {
  key: string; title: string; content: string; mode: 'replace' | 'append';
  baseRevision: number | null; baseKey: string; unknownKey: string;
};

function readDraft(key: string): MemoryDraft | undefined {
  try {
    const value = JSON.parse(sessionStorage.getItem(key) || 'null');
    if (value && ['key', 'title', 'content', 'baseKey', 'unknownKey'].every(field => typeof value[field] === 'string')
      && ['replace', 'append'].includes(value.mode)
      && (value.baseRevision === null || Number.isInteger(value.baseRevision))) return value;
  } catch { /* Draft storage is optional. */ }
}

function storeDraft(key: string, value?: MemoryDraft) {
  try {
    if (value) sessionStorage.setItem(key, JSON.stringify(value));
    else sessionStorage.removeItem(key);
  } catch { /* The mounted editor still retains the draft. */ }
}

export function buildTeamMemoryQueryKey(workspaceKey: string, scopeKey = ''): string[] {
  return ['team-memory', scopeKey, workspaceKey];
}

export function buildTeamMemoryEntryQueryKey(workspaceKey: string, entryKey: string, scopeKey = ''): string[] {
  return ['team-memory-entry', scopeKey, workspaceKey, entryKey];
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
  const user = useAuthStore(state => state.user);
  const tenantId = useAssistantTenant(user?.tenant_id ?? null);
  if (!user || !tenantId) return null;
  const scopeKey = `${user.id}:${tenantId}`;
  return <WorkspaceChoice key={`${scopeKey}:${agentId}`} agentId={agentId} section={section}
    tenantId={tenantId} userId={user.id} scopeKey={scopeKey} />;
}

function WorkspaceChoice({ agentId, section, tenantId, userId, scopeKey }: TeamMemorySummaryCardProps & {
  tenantId: string; userId: string; scopeKey: string;
}) {
  const { t } = useTranslation();
  const [selected, setSelected] = React.useState('');
  const choices = useQuery({ queryKey: ['team-memory-workspaces', scopeKey, agentId], retry: false,
    queryFn: async () => {
      const value = await memoryApi.listWorkspaces(agentId, tenantId);
      if (value.tenant_id !== tenantId || value.user_id !== userId) throw new Error(t('agent.workspace.sharedMemoryAccessUnavailable'));
      return value;
    } });
  const workspaces = choices.isError || choices.isFetching ? [] : choices.data?.workspaces ?? [];
  const workspace = workspaces.find(value => value.workspace_key === selected)
    ?? (!selected && workspaces.length === 1 ? workspaces[0] : undefined);
  return <div className="team-memory-card">
    <label>{t('agent.workspace.sharedMemoryTeam')}
      <select className="team-memory-field" value={workspace?.workspace_key ?? ''}
        onChange={event => setSelected(event.target.value)} disabled={choices.isFetching}>
        <option value="">{t('agent.workspace.sharedMemorySelectTeam')}</option>
        {workspaces.map(value => <option key={value.workspace_key} value={value.workspace_key}>{value.name}</option>)}
      </select>
    </label>
    {(choices.isError || choices.data?.unavailable) && <div role="alert">
      <p>{t('agent.workspace.sharedMemoryTeamsUnavailable')}</p>
      <button className="btn btn-secondary" type="button" onClick={() => { void choices.refetch(); }}>{t('common.retry')}</button>
    </div>}
    {choices.isFetching ? <p role="status">{t('common.loading')}</p>
      : !choices.isError && !workspaces.length ? <p>{t('agent.workspace.sharedMemoryNoTeams')}</p> : null}
    {workspace && <WorkspaceMemory key={`${scopeKey}:${workspace.workspace_key}`} agentId={agentId}
      section={section} workspace={workspace} tenantId={tenantId} scopeKey={scopeKey} />}
  </div>;
}

function WorkspaceMemory({ agentId, section, workspace, tenantId, scopeKey }: TeamMemorySummaryCardProps & {
  workspace: TeamMemoryWorkspace; tenantId: string; scopeKey: string;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [editing, setEditing] = React.useState(section === 'workspace');
  const isWorkspaceSection = editing && workspace.can_write;
  const [errorMessage, setErrorMessage] = React.useState('');
  const [syncStatus, setSyncStatus] = React.useState<'idle' | 'synced' | 'conflict' | 'error'>('idle');
  const memoryQuery = useQuery({
    queryKey: buildTeamMemoryQueryKey(workspace.workspace_key, scopeKey),
    queryFn: async () => {
      const values = await memoryApi.listShared(workspace.workspace_key, tenantId);
      if (values.some(value => value.tenant_id !== tenantId || value.workspace_key !== workspace.workspace_key))
        throw new Error(t('agent.workspace.sharedMemoryAccessUnavailable'));
      return values;
    },
    enabled: !!agentId,
  });
  const entries = memoryQuery.isError || memoryQuery.isFetching ? [] : memoryQuery.data ?? [];

  const [searchValue, setSearchValue] = React.useState('');
  const [selectedKey, setSelectedKey] = React.useState(entries[0]?.key ?? '');
  const filteredEntries = entries.filter((entry) => {
    const needle = searchValue.trim().toLowerCase();
    if (!needle) return true;
    return `${entry.title}\n${entry.snippet ?? ''}`.toLowerCase().includes(needle);
  });
  const selectedEntryKey = selectedKey || filteredEntries[0]?.key || entries[0]?.key || '';

  const entryQuery = useQuery({
    queryKey: buildTeamMemoryEntryQueryKey(workspace.workspace_key, selectedEntryKey, scopeKey),
    queryFn: async () => {
      const value = await memoryApi.getShared(selectedEntryKey, workspace.workspace_key, tenantId);
      if (value.tenant_id !== tenantId || value.workspace_key !== workspace.workspace_key)
        throw new Error(t('agent.workspace.sharedMemoryAccessUnavailable'));
      return value;
    },
    enabled: !!agentId && !!selectedEntryKey,
  });
  const accessUnavailable = memoryQuery.isError || entryQuery.isError;
  const selectedEntry = !accessUnavailable && !entryQuery.isFetching && entries.some(entry => entry.key === selectedEntryKey)
    ? entryQuery.data : undefined;
  const visibleEntries = filteredEntries.filter(entry => entry.key !== selectedEntryKey || (!accessUnavailable && !entryQuery.isFetching));

  React.useEffect(() => {
    if (memoryQuery.isError && memoryQuery.data !== undefined) {
      queryClient.getQueryCache().find({ queryKey: buildTeamMemoryQueryKey(workspace.workspace_key, scopeKey), exact: true })?.setState({ data: undefined });
    }
    if (entryQuery.isError && selectedEntryKey) {
      if (entryQuery.data !== undefined) {
        queryClient.getQueryCache().find({ queryKey: buildTeamMemoryEntryQueryKey(workspace.workspace_key, selectedEntryKey, scopeKey), exact: true })?.setState({ data: undefined });
      }
      queryClient.setQueryData<TeamMemoryEntry[]>(buildTeamMemoryQueryKey(workspace.workspace_key, scopeKey), current =>
        current?.some(entry => entry.key === selectedEntryKey) ? current.filter(entry => entry.key !== selectedEntryKey) : current);
    }
  }, [entryQuery.isError, entryQuery.data, memoryQuery.isError, memoryQuery.data, queryClient, selectedEntryKey, scopeKey, workspace.workspace_key]);

  const draftStorageKey = `team-memory-draft:${scopeKey}:${workspace.workspace_key}`;
  const initialDraft = React.useMemo(() => readDraft(draftStorageKey), [draftStorageKey]);
  const [hasDraft, setHasDraft] = React.useState(!!initialDraft);
  const [draftKey, setDraftKey] = React.useState(initialDraft?.key ?? '');
  const [draftTitle, setDraftTitle] = React.useState(initialDraft?.title ?? '');
  const [draftContent, setDraftContent] = React.useState(initialDraft?.content ?? '');
  const [saveMode, setSaveMode] = React.useState<'replace' | 'append'>(initialDraft?.mode ?? 'replace');
  const [draftBase, setDraftBase] = React.useState({ key: initialDraft?.baseKey ?? '', revision: initialDraft?.baseRevision ?? null });
  const [unknownKey, setUnknownKey] = React.useState(initialDraft?.unknownKey ?? '');
  const [conflictEntry, setConflictEntry] = React.useState<TeamMemoryEntry | null>(null);

  React.useEffect(() => {
    if (hasDraft) storeDraft(draftStorageKey, { key: draftKey, title: draftTitle, content: draftContent, mode: saveMode,
      baseKey: draftBase.key, baseRevision: draftBase.revision, unknownKey });
  }, [hasDraft, draftStorageKey, draftKey, draftTitle, draftContent, saveMode, draftBase, unknownKey]);

  React.useEffect(() => {
    if (!selectedKey && entries.length > 0) {
      setSelectedKey(entries[0].key);
    }
  }, [entries, selectedKey]);

  React.useEffect(() => {
    if (!isWorkspaceSection || !selectedEntry || hasDraft) {
      return;
    }
    setDraftKey(selectedEntry.key);
    setDraftTitle(selectedEntry.title);
    setDraftContent(selectedEntry.content ?? '');
    setSaveMode('replace');
    setDraftBase({ key: selectedEntry.key, revision: selectedEntry.revision ?? null });
  }, [isWorkspaceSection, selectedEntry, hasDraft]);

  function saved(entry: TeamMemoryEntry) {
    storeDraft(draftStorageKey);
    setHasDraft(false);
    setUnknownKey('');
    setConflictEntry(null);
    setDraftBase({ key: entry.key, revision: entry.revision ?? null });
    setErrorMessage('');
    setSyncStatus('synced');
    void queryClient.invalidateQueries({ queryKey: buildTeamMemoryQueryKey(workspace.workspace_key, scopeKey) });
    void queryClient.invalidateQueries({ queryKey: buildTeamMemoryEntryQueryKey(workspace.workspace_key, entry.key, scopeKey) });
    setSelectedKey(entry.key);
  }

  async function checkSaved() {
    try {
      const entry = await memoryApi.getShared(draftKey, workspace.workspace_key, tenantId);
      if (entry.tenant_id !== tenantId || entry.workspace_key !== workspace.workspace_key) throw new Error('Scope changed');
      if (entry.content === draftContent && entry.title === draftTitle && saveMode === 'replace') saved(entry);
      else setConflictEntry(entry);
    } catch (error) { setErrorMessage(formatTeamMemoryMutationError(error, t('agent.workspace.sharedMemorySaveError'))); }
  }

  const saveMutation = useMutation({
    mutationFn: async () => {
      const entry = await memoryApi.upsertShared(
        buildTeamMemoryUpsertRequest(
          workspace.workspace_key,
          {
            key: draftKey,
            title: draftTitle,
            content: draftContent,
            mode: saveMode,
          },
          draftBase.key === draftKey ? { revision: draftBase.revision ?? undefined } : null,
        ),
        tenantId,
      );
      if (entry.tenant_id !== tenantId || entry.workspace_key !== workspace.workspace_key) throw new Error('Scope changed');
      return entry;
    },
    onSuccess: saved,
    onError: (error) => {
      const message = formatTeamMemoryMutationError(
        error,
        t('agent.workspace.sharedMemorySaveError', 'Failed to save shared memory entry.'),
      );
      const data = error instanceof ApiError && error.data && typeof error.data === 'object'
        ? error.data as { outcome?: string; current_entry?: TeamMemoryEntry } : undefined;
      const current = data?.current_entry;
      if (current && current.tenant_id === tenantId && current.workspace_key === workspace.workspace_key && current.key === draftKey)
        setConflictEntry(current);
      const uncertain = !(error instanceof ApiError) || error.status === 0 || data?.outcome === 'unknown';
      if (uncertain) {
        setUnknownKey(draftKey);
        storeDraft(draftStorageKey, { key: draftKey, title: draftTitle, content: draftContent, mode: saveMode,
          baseKey: draftBase.key, baseRevision: draftBase.revision, unknownKey: draftKey });
      }
      if (error instanceof ApiError && [403, 404].includes(error.status)) {
        void memoryQuery.refetch();
        if (selectedEntryKey) void entryQuery.refetch();
      }
      setSyncStatus(current ? 'conflict' : 'error');
      setErrorMessage(message);
    },
  });

  const deleteMutation = useMutation({
    mutationFn: async () => memoryApi.deleteShared(selectedEntryKey, workspace.workspace_key, tenantId, selectedEntry?.revision),
    onSuccess: () => {
      setErrorMessage('');
      setSyncStatus('synced');
      queryClient.invalidateQueries({ queryKey: buildTeamMemoryQueryKey(workspace.workspace_key, scopeKey) });
      if (selectedEntryKey) {
        queryClient.invalidateQueries({ queryKey: buildTeamMemoryEntryQueryKey(workspace.workspace_key, selectedEntryKey, scopeKey) });
      }
      setSelectedKey('');
      setDraftKey('');
      setDraftTitle('');
      setDraftContent('');
      storeDraft(draftStorageKey);
      setHasDraft(false);
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
        {workspace.can_write && <button className="btn btn-secondary" type="button" onClick={() => setEditing(value => !value)}>
          {isWorkspaceSection ? t('common.close', 'Close') : t('common.edit', 'Edit')}
        </button>}
      </div>

      <input
        value={searchValue}
        onChange={(event) => setSearchValue(event.target.value)}
        placeholder={t(searchPlaceholderKey, 'Search shared memory')}
        className="team-memory-search"
      />

      <div className={`team-memory-grid${isWorkspaceSection ? ' is-workspace' : ''}`}>
        <div className="team-memory-list">
          {visibleEntries.length === 0 ? (
            <div className="team-memory-empty">
              {t(emptyKey, 'No shared memory entries yet.')}
            </div>
          ) : (
            visibleEntries.map((entry) => {
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
                <div className="team-memory-detail-title">{selectedEntry?.title ?? visibleEntries[0]?.title ?? t(emptyKey, 'No shared memory entries yet.')}</div>
              </div>
              {selectedEntry?.updated_at && (
                <span className="team-memory-detail-updated">
                  {t(updatedKey, { time: formatUpdatedAt(selectedEntry.updated_at), defaultValue: 'Updated {{time}}' })}
                </span>
              )}
            </div>
            <div className="team-memory-detail-content">
              {accessUnavailable ? (
                <div role="alert">
                  <p>{t('agent.workspace.sharedMemoryAccessUnavailable')}</p>
                  <button type="button" className="btn btn-secondary" onClick={() => {
                    void memoryQuery.refetch();
                    void entryQuery.refetch();
                  }}>{t('common.retry')}</button>
                </div>
              ) : entryQuery.isFetching || memoryQuery.isFetching ? t('common.loading') : (
                selectedEntry?.content ?? selectedEntry?.snippet ?? visibleEntries[0]?.snippet ?? t(emptyKey, 'No shared memory entries yet.')
              )}
            </div>
            {selectedEntry && (
              <div className="team-memory-detail-rev">
                {`rev ${selectedEntry.revision ?? 0}${selectedEntry.updated_by ? ` · ${selectedEntry.updated_by}` : ''}`}
              </div>
            )}
          </div>

          {isWorkspaceSection && !accessUnavailable && !entryQuery.isFetching && !memoryQuery.isFetching && (
            <div className="team-memory-panel team-memory-editor">
              <input
                value={draftKey}
                onChange={(event) => { setHasDraft(true); setDraftKey(event.target.value); }}
                placeholder={t('agent.workspace.sharedMemoryKeyPlaceholder', 'Entry key')}
                className="team-memory-field"
              />
              <input
                value={draftTitle}
                onChange={(event) => { setHasDraft(true); setDraftTitle(event.target.value); }}
                placeholder={t('agent.workspace.sharedMemoryTitlePlaceholder', 'Entry title')}
                className="team-memory-field"
              />
              <textarea
                value={draftContent}
                onChange={(event) => { setHasDraft(true); setDraftContent(event.target.value); }}
                placeholder={t('agent.workspace.sharedMemoryContentPlaceholder', 'Write the shared note or playbook here')}
                rows={6}
                className="team-memory-textarea"
              />
              <div className="team-memory-mode-row">
                <select
                  value={saveMode}
                  onChange={(event) => { setHasDraft(true); setSaveMode(event.target.value as 'replace' | 'append'); }}
                  className="team-memory-select"
                >
                  <option value="replace">{t('agent.workspace.sharedMemoryModeReplace', 'Replace')}</option>
                  <option value="append">{t('agent.workspace.sharedMemoryModeAppend', 'Append')}</option>
                </select>
                <button type="button" className="btn btn-primary" onClick={() => saveMutation.mutate()} disabled={saveMutation.isPending || !draftKey || !draftTitle || !draftContent || (!!unknownKey && unknownKey === draftKey)}>
                  {t('agent.workspace.sharedMemorySave', 'Save to shared memory')}
                </button>
                <button type="button" className="btn btn-ghost" onClick={handleDelete} disabled={!workspace.can_delete || deleteMutation.isPending || !selectedEntryKey}>
                  {t('agent.workspace.sharedMemoryDelete', 'Delete entry')}
                </button>
              </div>
              {hasDraft && <button className="btn btn-ghost" type="button" onClick={() => {
                storeDraft(draftStorageKey); setHasDraft(false); setUnknownKey(''); setConflictEntry(null);
                setDraftKey(selectedEntry?.key ?? ''); setDraftTitle(selectedEntry?.title ?? ''); setDraftContent(selectedEntry?.content ?? '');
                setDraftBase({ key: selectedEntry?.key ?? '', revision: selectedEntry?.revision ?? null });
              }}>{t('agent.workspace.sharedMemoryDiscardDraft')}</button>}
              {!!unknownKey && unknownKey === draftKey && <div role="status">
                <p>{t('agent.workspace.sharedMemorySaveUnknown')}</p>
                <button className="btn btn-secondary" type="button" onClick={() => { void checkSaved(); }}>{t('agent.workspace.sharedMemoryCheckSaved')}</button>
              </div>}
              {conflictEntry && <div className="team-memory-panel">
                <p>{t('agent.workspace.sharedMemoryCurrentVersion')}</p>
                <pre className="team-memory-detail-content">{conflictEntry.content}</pre>
                <button className="btn btn-secondary" type="button" onClick={() => {
                  setHasDraft(true); setDraftBase({ key: conflictEntry.key, revision: conflictEntry.revision ?? null });
                  setConflictEntry(null); setUnknownKey(''); setErrorMessage('');
                }}>{t('agent.workspace.sharedMemoryUseLatestBase')}</button>
              </div>}
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
