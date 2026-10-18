import { useEffect, useId, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { channelApi, type FeishuSharedGroupEntry } from '../api/domains/channels';
import { useAuthStore } from '../stores';

export default function FeishuSharedGroups({ agentId }: { agentId: string }) {
  const user = useAuthStore((state) => state.user);
  const [generation, setGeneration] = useState(0);
  useEffect(() => {
    const changed = (event: StorageEvent) => { if (!event.key || event.key === 'current_tenant_id') setGeneration((value) => value + 1); };
    window.addEventListener('storage', changed);
    return () => window.removeEventListener('storage', changed);
  }, []);
  let tenant = user?.tenant_id || '';
  try { tenant = localStorage.getItem('current_tenant_id') || tenant; } catch { /* Storage is optional. */ }
  if (!user) return null;
  const scope = `${tenant}:${user.id}:${agentId}:${generation}`;
  return <SharedGroupEditor key={scope} agentId={agentId} scope={scope} />;
}

function SharedGroupEditor({ agentId, scope }: { agentId: string; scope: string }) {
  const { t } = useTranslation();
  const id = useId();
  const client = useQueryClient();
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState('');
  const [base, setBase] = useState<FeishuSharedGroupEntry | null>(null);
  const key = ['feishu-shared-groups', scope];
  const query = useQuery({ queryKey: key, queryFn: () => channelApi.feishuSharedGroups(agentId), enabled: open });
  useEffect(() => {
    if (query.data && !base) { setBase(query.data); setDraft(query.data.chat_ids.join('\n')); }
  }, [query.data, base]);
  const save = useMutation({
    mutationFn: () => channelApi.saveFeishuSharedGroups(agentId, base!.revision, draft.split(/\s+/).filter(Boolean)),
    onSuccess: (value) => { setBase(value); setDraft(value.chat_ids.join('\n')); client.setQueryData(key, value); },
  });
  async function refresh() {
    const result = await query.refetch();
    if (result.data && !result.isError) {
      if (!base) setDraft(result.data.chat_ids.join('\n'));
      setBase(result.data); save.reset();
    }
  }
  return <details open={open} onToggle={(event) => setOpen(event.currentTarget.open)} className="channel-config-info-block channel-config-group-entry">
    <summary>{t('feishuSharedGroups.title', 'Shared group work')}</summary>
    {open && <>
      <p>{t('feishuSharedGroups.description', 'Choose the group IDs whose new messages should enter one shared work conversation. Current group membership and employee permissions still apply. Other groups and direct messages keep their separate conversations.')}</p>
      <p>{t('feishuSharedGroups.history', 'Existing private history stays private. Removing a group changes new message entry; accepted work and earlier shared history remain available to authorized members.')}</p>
      {query.isError && <p role="alert">{t('feishuSharedGroups.readFailed', 'Could not read the current setting. Your draft is kept; refresh to retry.')}</p>}
      {save.isError && <p role="alert">{t('feishuSharedGroups.saveFailed', 'Could not confirm this save. Refresh the saved list, compare it with your draft, then save again if needed.')}</p>}
      {query.data && !query.isError && <p>{t('feishuSharedGroups.saved', 'Saved groups')}: {query.data.chat_ids.join(', ') || t('feishuSharedGroups.none', 'None')}</p>}
      <label htmlFor={id} className="channel-config-field-label">{t('feishuSharedGroups.ids', 'Group IDs, one per line')}</label>
      <textarea id={id} className="input channel-config-field-input" value={draft} onChange={(event) => { setDraft(event.target.value); save.reset(); }}
        rows={4} disabled={!base || save.isPending} spellCheck={false} />
      <div className="channel-config-hstack-sm">
        <button type="button" className="btn btn-secondary" onClick={() => { void refresh(); }} disabled={query.isFetching || save.isPending}>
          {t('common.refresh', 'Refresh')}</button>
        <button type="button" className="btn btn-primary" onClick={() => save.mutate()} disabled={!base || query.isError || query.isFetching || save.isPending}>
          {t('common.save', 'Save')}</button>
      </div>
      {save.isSuccess && <p role="status">{t('feishuSharedGroups.confirmed', 'Group entry setting saved.')}</p>}
    </>}
  </details>;
}
