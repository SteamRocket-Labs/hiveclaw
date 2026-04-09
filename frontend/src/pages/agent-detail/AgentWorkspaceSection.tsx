import React from 'react';

import FileBrowser, { type FileBrowserApi } from '../../components/FileBrowser';
import { fileApi } from '../../api/domains/files';
import TeamMemorySummaryCard from './TeamMemorySummaryCard';

type AgentWorkspaceSectionProps = {
  agentId: string;
};

export default function AgentWorkspaceSection({ agentId }: AgentWorkspaceSectionProps) {
  const adapter: FileBrowserApi = {
    list: (path) => fileApi.list(agentId, path),
    read: (path) => fileApi.read(agentId, path),
    write: (path, content) => fileApi.write(agentId, path, content),
    delete: (path) => fileApi.delete(agentId, path),
    upload: (file, path, onProgress) => fileApi.upload(agentId, file, `${path}/`, onProgress),
    downloadUrl: (path) => fileApi.downloadUrl(agentId, path),
  };

  return (
    <div style={{ padding: '20px 24px' }}>
      <TeamMemorySummaryCard agentId={agentId} section="workspace" />
      <FileBrowser
        api={adapter}
        rootPath="workspace"
        features={{ upload: true, newFile: true, newFolder: true, edit: true, delete: true, directoryNavigation: true }}
      />
    </div>
  );
}
