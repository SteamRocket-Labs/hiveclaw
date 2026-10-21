import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { IconDownload } from '@tabler/icons-react';

import { ApiError } from '../../api/core';
import { chatApi } from '../../api/domains/chat';
import { showAppToast } from '../../components/AppDialogs';
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
  async function discover() {
    const frame = authFrame();
    try {
      const result = await chatApi.findOriginalWorkspaceUpload(agentId, path);
      if (alive.current && frame === authFrame()) { setRefId(result.id); setRetry(false); }
    } catch (error) {
      if (alive.current && frame === authFrame()) setRetry(!(error instanceof ApiError && [403, 404].includes(error.status)));
    }
  }
  useEffect(() => {
    alive.current = true;
    if (!original) void discover();
    return () => { alive.current = false; };
    // The keyed instance owns one reader/source identity, including A-B-A switches.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  if (!refId && !retry) return null;
  return <button type="button" className="chat-artifact-action" disabled={busy} onClick={async (event) => {
    event.stopPropagation();
    if (inflight.current) return;
    inflight.current = true; setBusy(true);
    try {
      if (!refId) await discover();
      else {
        const frame = authFrame();
        const blob = await chatApi.downloadOriginalUpload(refId);
        if (alive.current && frame === authFrame()) saveBlob(blob, name);
      }
    } catch (error) {
      if (alive.current) showAppToast(t('agent.chat.artifacts.downloadFailed', 'Download failed: {{message}}', {
        message: error instanceof Error ? error.message : String(error),
      }), 'error');
    } finally { inflight.current = false; if (alive.current) setBusy(false); }
  }}>
    <IconDownload size={12} />{retry && !refId ? t('common.retry', 'Retry') : t('agent.chat.artifacts.originalUpload', 'Original upload')}
  </button>;
}
