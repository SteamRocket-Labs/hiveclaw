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
  const authority = operatorView
    ? { operatorView: true, reason: 'Agent workspace administration' }
    : undefined;
  const adapter: FileBrowserApi = {
    list: (path) => fileApi.list(agentId, path, authority),
    read: (path) => fileApi.read(agentId, path, authority),
    write: (path, content) => fileApi.write(agentId, path, content, authority),
    delete: (path) => fileApi.delete(agentId, path, authority),
    upload: (file, path, onProgress) => fileApi.upload(agentId, file, `${path}/`, onProgress, authority),
    downloadUrl: (path) => fileApi.downloadUrl(agentId, path, authority),
  };

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
