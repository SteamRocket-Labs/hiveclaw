import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { IconDownload } from '@tabler/icons-react';

import { ApiError } from '../../api/core';
import { chatApi, type OriginalReferenceStatus } from '../../api/domains/chat';
import { requestAppConfirm, showAppToast } from '../../components/AppDialogs';
import { saveBlob } from '../../utils/authenticatedResource';

interface Props {
  agentId: string;
  path: string;
  name: string;
  viewerUserId?: string;
  original?: { id: string; ownerUserId: string };
  discover?: boolean;
}

function authFrame() {
  try { return JSON.stringify([localStorage.getItem('token'), localStorage.getItem('current_tenant_id')]); }
  catch { return null; }
}

export default function OriginalUploadAction(props: Props) {
  if (!props.viewerUserId || (props.original && props.original.ownerUserId !== props.viewerUserId)
    || (!props.original && !props.discover)) return null;
  let tenant: string | null = null;
  try { tenant = localStorage.getItem('current_tenant_id'); } catch { /* Authenticated transport remains authoritative. */ }
  return <OriginalForReader key={JSON.stringify([props.viewerUserId, tenant, props.agentId, props.path, props.original?.id])} {...props} />;
}

function OriginalForReader({ agentId, path, name, original }: Props) {
  const { t } = useTranslation();
  const alive = useRef(true);
  const inflight = useRef(false);
  const [refId, setRefId] = useState(original?.id);
  const [retry, setRetry] = useState(false);
  const [busy, setBusy] = useState(false);
  const [info, setInfo] = useState<OriginalReferenceStatus | null>(null);
  const [details, setDetails] = useState(false);
  const [statusFailed, setStatusFailed] = useState(false);
  const [withdrawn, setWithdrawn] = useState(false);
  async function discover() {
    const frame = authFrame();
    try {
      const result = await chatApi.findOriginalWorkspaceUpload(agentId, path);
      if (alive.current && frame === authFrame()) { setRefId(result.id); setRetry(false); }
    } catch (error) {
      if (alive.current && frame === authFrame()) setRetry(!(error instanceof ApiError && [403, 404, 410].includes(error.status)));
    }
  }
  useEffect(() => {
    alive.current = true;
    if (!original) void discover();
    return () => { alive.current = false; };
    // The keyed instance owns one reader/source identity, including A-B-A switches.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  async function loadStatus() {
    if (!refId || inflight.current) return;
    const frame = authFrame();
    inflight.current = true; setBusy(true); setDetails(true);
    try {
      const result = await chatApi.getOriginalReferenceStatus(refId);
      if (alive.current && frame === authFrame()) {
        setInfo(result); setStatusFailed(false); setWithdrawn(result.state === 'withdrawn');
      }
    } catch { if (alive.current && frame === authFrame()) setStatusFailed(true); }
    finally { inflight.current = false; if (alive.current) setBusy(false); }
  }
  async function withdraw() {
    if (!refId || !info?.can_withdraw || inflight.current) return;
    const frame = authFrame();
    inflight.current = true; setBusy(true);
    try {
      const confirmed = await requestAppConfirm({ title: t('agent.chat.artifacts.withdrawOriginal', 'Withdraw original'),
        message: t('agent.chat.artifacts.withdrawOriginalConfirm', 'Stop future use of this original? Working files stay unchanged. History and retained data are not deleted.'),
        confirmLabel: t('agent.chat.artifacts.withdrawOriginal', 'Withdraw original'), danger: true });
      if (!confirmed || !alive.current || frame !== authFrame()) return;
      const receipt = await chatApi.withdrawOriginalUpload(refId, info.content_sha256);
      if (receipt.state !== 'withdrawn') throw new Error('original_withdrawal_unconfirmed');
      if (alive.current && frame === authFrame()) setWithdrawn(true);
    } catch {
      if (alive.current && frame === authFrame()) showAppToast(t('agent.chat.artifacts.withdrawOriginalFailed',
        'Could not confirm withdrawal. Check Original status before retrying.'), 'error');
    } finally { inflight.current = false; if (alive.current) setBusy(false); }
  }
  if (!refId && !retry) return null;
  return <>
    {withdrawn && <span role="status">{t('agent.chat.artifacts.sourceWithdrawn', 'Original withdrawn. Select an authorized source to continue.')}</span>}
    {(!withdrawn || info?.can_inspect_retained) && <button type="button" className="chat-artifact-action" disabled={busy} onClick={async (event) => {
    event.stopPropagation();
    if (inflight.current) return;
    inflight.current = true; setBusy(true);
    const frame = authFrame();
    try {
      if (!refId) await discover();
      else {
        const blob = await chatApi.downloadOriginalUpload(refId);
        if (alive.current && frame === authFrame()) saveBlob(blob, name);
      }
    } catch (error) {
      if (!alive.current || frame !== authFrame()) return;
      if (error instanceof ApiError && error.status === 410) { setWithdrawn(true); setInfo(null); return; }
      if (alive.current) showAppToast(t('agent.chat.artifacts.downloadFailed', 'Download failed: {{message}}', {
        message: error instanceof Error ? error.message : String(error),
      }), 'error');
    } finally { inflight.current = false; if (alive.current) setBusy(false); }
  }}>
    <IconDownload size={12} />{withdrawn ? t('agent.chat.artifacts.retainedOriginal', 'Inspect retained original')
      : retry && !refId ? t('common.retry', 'Retry') : t('agent.chat.artifacts.originalUpload', 'Original upload')}
  </button>}
    {refId && <button type="button" className="chat-artifact-action" disabled={busy} onClick={(event) => {
      event.stopPropagation(); void loadStatus();
    }}>{t('agent.chat.artifacts.originalStatus', 'Original status')}</button>}
    {details && (statusFailed || !withdrawn) && <span role="status">{statusFailed ? t('agent.chat.artifacts.originalStatusFailed', 'Could not load source status. Retry Original status.')
      : info ? t('agent.chat.artifacts.sourceActive', 'Original active') : t('common.loading', 'Loading...')}</span>}
    {details && !withdrawn && !statusFailed && info?.can_withdraw && <button type="button" className="chat-artifact-action" disabled={busy}
      onClick={(event) => { event.stopPropagation(); void withdraw(); }}>{t('agent.chat.artifacts.withdrawOriginal', 'Withdraw original')}</button>}
  </>;
}
