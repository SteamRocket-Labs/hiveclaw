import React from 'react';

import FileBrowser, { type FileBrowserApi } from '../../components/FileBrowser';
import { fileApi } from '../../api/domains/files';
import './AgentWorkspaceSection.css';

type AgentWorkspaceSectionProps = {
  agentId: string;
  canUseOperatorView?: boolean;
};

export default function AgentWorkspaceSection({ agentId, canUseOperatorView = false }: AgentWorkspaceSectionProps) {
  const [operatorView, setOperatorView] = React.useState(false);
  React.useEffect(() => setOperatorView(false), [agentId]);
  const adapter = React.useMemo<FileBrowserApi>(() => {
    const authority = operatorView
      ? { operatorView: true, reason: 'Agent workspace administration' }
      : undefined;
    return {
      list: (path) => fileApi.list(agentId, path, authority),
      read: (path) => fileApi.read(agentId, path, authority),
      versions: (path, offset, limit) => fileApi.versions(agentId, path, offset, limit, authority),
      readVersion: (path, versionId) => fileApi.readVersion(agentId, path, versionId, authority),
      restoreVersion: (path, versionId, request) =>
        fileApi.restoreVersion(agentId, path, versionId, request, authority),
      downloadVersion: (path, versionId) => fileApi.downloadVersion(agentId, path, versionId, authority),
      write: (path, content) => fileApi.write(agentId, path, content, authority),
      delete: (path) => fileApi.delete(agentId, path, authority),
      upload: (file, path, onProgress) => fileApi.upload(agentId, file, `${path}/`, onProgress, authority),
      download: (path) => fileApi.download(agentId, path, authority),
    };
  }, [agentId, operatorView]);

  return (
    <div style={{ padding: '20px 24px' }}>
      {canUseOperatorView ? (
        <div className="agent-detail-operator-controls">
          <button
            type="button"
            className="btn btn-secondary"
            aria-pressed={operatorView}
            onClick={() => setOperatorView((current) => !current)}
          >
            {operatorView ? 'Exit operator view' : 'Enter operator view'}
          </button>
        </div>
      ) : null}
      {operatorView ? (
        <div className="agent-detail-governance-note" role="status">
          <strong>Operator view</strong> · You are viewing tenant-wide workspace resources. Every cross-owner action is audited.
        </div>
      ) : null}
      <FileBrowser
        key={operatorView ? 'operator' : 'owner'}
        api={adapter}
        rootPath="workspace"
        features={{ upload: true, newFile: true, newFolder: true, edit: true, delete: true, directoryNavigation: true }}
      />
    </div>
  );
}
