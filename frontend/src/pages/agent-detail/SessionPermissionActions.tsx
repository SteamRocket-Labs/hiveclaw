import React from 'react';
import type { SessionPermissionRequest } from './chatRuntime';

export function permissionOnceOnlyMessageKey(request: SessionPermissionRequest): string {
  const destructive = request.destructive === true
    || request.risk_class === 'destructive_delete'
    || request.confirmation_kind === 'destructive_once';
  return destructive ? 'agent.chat.permission.deleteOnceOnly' : 'agent.chat.permission.onceOnly';
}

export default function SessionPermissionActions({
  permissionRequest,
  onResolveSessionPermission,
  readOnly = false,
  t,
}: {
  permissionRequest: SessionPermissionRequest;
  onResolveSessionPermission?: (request: SessionPermissionRequest, action: 'allow_once' | 'allow_session' | 'deny') => void | Promise<unknown>;
  readOnly?: boolean;
  t: (key: string, fallback: string) => string;
}) {
  const [pending, setPending] = React.useState(false);
  const [error, setError] = React.useState('');
  const resolve = async (action: 'allow_once' | 'allow_session' | 'deny') => {
    if (pending || readOnly || !onResolveSessionPermission) return;
    setPending(true);
    setError('');
    try {
      await onResolveSessionPermission(permissionRequest, action);
    } catch (err) {
      setError(err instanceof Error ? err.message : t('agent.chat.permission.resolveFailed', 'Failed to resolve permission request'));
    } finally {
      setPending(false);
    }
  };
  if (readOnly) return <p role="status">{t('agent.chat.permission.waitingForRequester', 'Waiting for the original requester to decide.')}</p>;
  const allowSession =
    permissionRequest.allow_session_allowed !== false
    && permissionRequest.risk_class !== 'destructive_delete'
    && permissionRequest.confirmation_kind !== 'destructive_once';
  return (
    <>
      <div style={{ marginTop: '10px', display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
        <button
          type="button"
          className="btn btn-secondary"
          disabled={pending || !onResolveSessionPermission}
          onClick={() => void resolve('allow_once')}
          style={{ fontSize: '12px', padding: '6px 10px' }}
        >
          {t('agent.chat.permission.allowOnce', 'Allow once')}
        </button>
        {allowSession && (
          <button
            type="button"
            className="btn btn-secondary"
            disabled={pending || !onResolveSessionPermission}
            onClick={() => void resolve('allow_session')}
            style={{ fontSize: '12px', padding: '6px 10px' }}
          >
            {t('agent.chat.permission.allowSession', 'Allow for this session')}
          </button>
        )}
        <button
          type="button"
          className="btn btn-secondary"
          disabled={pending || !onResolveSessionPermission}
          onClick={() => void resolve('deny')}
          style={{ fontSize: '12px', padding: '6px 10px' }}
        >
          {t('agent.chat.permission.deny', 'Deny')}
        </button>
      </div>
      {pending && <p role="status">{t('agent.chat.permission.applying', 'Applying decision…')}</p>}
      {error && <p role="alert">{error}</p>}
      {!allowSession && (
        <div style={{ marginTop: '7px', fontSize: '11px', color: 'var(--text-tertiary)' }}>
          {permissionOnceOnlyMessageKey(permissionRequest) === 'agent.chat.permission.deleteOnceOnly'
            ? t('agent.chat.permission.deleteOnceOnly', 'Delete actions can only be allowed once.')
            : t('agent.chat.permission.onceOnly', 'This action can only be allowed once.')}
        </div>
      )}
    </>
  );
}
