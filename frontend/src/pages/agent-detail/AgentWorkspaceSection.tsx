import React from 'react';
import { useTranslation } from 'react-i18next';

import FileBrowser, { type FileBrowserApi } from '../../components/FileBrowser';
import { fileApi } from '../../api/domains/files';
import './AgentWorkspaceSection.css';

type AgentWorkspaceSectionProps = {
  agentId: string;
  canUseOperatorView?: boolean;
  operatorOnly?: boolean;
  operatorReason?: string;
  onOperatorAuthorityDenied?: (error: unknown) => void;
};

function isForbiddenOperatorResponse(error: unknown): boolean {
  if (!error || typeof error !== 'object') return false;
  return Number((error as { status?: unknown }).status) === 403;
}

export default function AgentWorkspaceSection({
  agentId,
  canUseOperatorView = false,
  operatorOnly = false,
  operatorReason,
  onOperatorAuthorityDenied,
}: AgentWorkspaceSectionProps) {
  const { t } = useTranslation();
  const normalizedOperatorReason = operatorReason?.trim() ?? '';
  const operatorAuthorityScope = canUseOperatorView && normalizedOperatorReason
    ? `${agentId}\u0000${normalizedOperatorReason}`
    : null;
  const [operatorViewScope, setOperatorViewScope] = React.useState<string | null>(null);
  const operatorView = Boolean(
    operatorAuthorityScope
    && (operatorOnly || operatorViewScope === operatorAuthorityScope),
  );
  React.useEffect(() => setOperatorViewScope(null), [agentId, canUseOperatorView, normalizedOperatorReason]);
  const guardOperatorRead = React.useCallback(async <T,>(request: Promise<T>): Promise<T> => {
    try {
      return await request;
    } catch (error) {
      if (operatorView && isForbiddenOperatorResponse(error)) onOperatorAuthorityDenied?.(error);
      throw error;
    }
  }, [onOperatorAuthorityDenied, operatorView]);
  const adapter = React.useMemo<FileBrowserApi>(() => {
    const authority = operatorView
      ? { operatorView: true, reason: normalizedOperatorReason }
      : undefined;
    return {
      list: (path) => guardOperatorRead(fileApi.list(agentId, path, authority)),
      read: (path) => guardOperatorRead(fileApi.read(agentId, path, authority)),
      versions: (path, offset, limit) => guardOperatorRead(fileApi.versions(agentId, path, offset, limit, authority)),
      readVersion: (path, versionId) => guardOperatorRead(fileApi.readVersion(agentId, path, versionId, authority)),
      restoreVersion: (path, versionId, request) =>
        operatorView
          ? Promise.reject(new Error('Operator View is read-only'))
          : fileApi.restoreVersion(agentId, path, versionId, request),
      downloadVersion: (path, versionId) => guardOperatorRead(fileApi.downloadVersion(agentId, path, versionId, authority)),
      write: (path, content) => operatorView
        ? Promise.reject(new Error('Operator View is read-only'))
        : fileApi.write(agentId, path, content),
      delete: (path) => operatorView
        ? Promise.reject(new Error('Operator View is read-only'))
        : fileApi.delete(agentId, path),
      upload: (file, path, onProgress) => operatorView
        ? Promise.reject(new Error('Operator View is read-only'))
        : fileApi.upload(agentId, file, `${path}/`, onProgress),
      download: (path) => guardOperatorRead(fileApi.download(agentId, path, authority)),
    };
  }, [agentId, guardOperatorRead, normalizedOperatorReason, operatorView]);

  if (operatorOnly && !operatorAuthorityScope) {
    return (
      <div className="agent-detail-placeholder" data-testid="operator-workspace-reason-gate" role="status">
        {canUseOperatorView
          ? t(
            'agent.operator.workspaceReasonRequired',
            'Enter and apply an inspection reason before viewing private workspace files.',
          )
          : t('agent.operator.workspaceUnavailable', 'Operator workspace inspection is unavailable.')}
      </div>
    );
  }

  return (
    <div style={{ padding: '20px 24px' }}>
      {operatorAuthorityScope && !operatorOnly ? (
        <div className="agent-detail-operator-controls">
          <button
            type="button"
            className="btn btn-secondary"
            aria-pressed={operatorView}
            onClick={() => setOperatorViewScope((current) => (
              current === operatorAuthorityScope ? null : operatorAuthorityScope
            ))}
          >
            {operatorView
              ? t('agent.operator.exitView', 'Exit operator view')
              : t('agent.operator.enterView', 'Enter operator view')}
          </button>
        </div>
      ) : null}
      {operatorView ? (
        <div className="agent-detail-governance-note" role="status">
          <strong>{t('agent.operator.viewTitle', 'Operator view')}</strong>
          {' · '}
          {t(
            'agent.operator.workspaceReadOnly',
            'Cross-owner reads are audited. Editing, upload, restore, and deletion are disabled.',
          )}
        </div>
      ) : null}
      <FileBrowser
        key={`${agentId}:${operatorView ? `operator:${normalizedOperatorReason}` : 'owner'}`}
        api={adapter}
        rootPath="workspace"
        features={{
          upload: !operatorView,
          newFile: !operatorView,
          newFolder: !operatorView,
          edit: !operatorView,
          delete: !operatorView,
          directoryNavigation: true,
        }}
      />
    </div>
  );
}
