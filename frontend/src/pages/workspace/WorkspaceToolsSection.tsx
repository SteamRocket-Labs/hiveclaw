import { lazy, Suspense, useState } from 'react';
import { useTranslation } from 'react-i18next';

import './WorkspaceToolsSection.css';

export {
  ToolConfigSecretListField,
  countToolConfigListValues,
  getWorkspaceProviderAuthDisplay,
  getWorkspaceToolGovernanceState,
  isExtensionOrAddonTool,
  normalizeToolConfigListValue,
  resolveWorkspaceToolCapability,
  sortWorkspaceToolsForDisplay,
} from './workspaceToolsModel';

const WorkspaceGlobalToolsView = lazy(() => import('./WorkspaceGlobalToolsView'));
const WorkspaceMcpServersView = lazy(() => import('./WorkspaceMcpServersView'));
const WorkspaceCustomApiView = lazy(() => import('./WorkspaceCustomApiView'));
const WorkspaceAgentInstalledToolsView = lazy(() => import('./WorkspaceAgentInstalledToolsView'));

type WorkspaceToolsView = 'global' | 'mcp-servers' | 'custom-api' | 'agent-installed';

export default function WorkspaceToolsSection({ selectedTenantId }: { selectedTenantId: string }) {
  const { t } = useTranslation();
  const [activeView, setActiveView] = useState<WorkspaceToolsView>('global');
  const tabs: Array<[WorkspaceToolsView, string]> = [
    ['global', t('enterprise.tools.extensionsAddons', 'Extensions & Add-ons')],
    ['mcp-servers', t('agent.extensions.mcpServers', 'MCP Servers')],
    ['custom-api', t('enterprise.tools.customApis', 'Custom APIs')],
    ['agent-installed', t('enterprise.tools.agentInstalled', 'Agent Installed')],
  ];

  return (
    <div>
      <div className="ws-tools-tabs">
        {tabs.map(([key, label]) => (
          <button key={key} type="button" onClick={() => setActiveView(key)} className={`ws-tools-tab ${activeView === key ? 'active' : ''}`}>
            {label}
          </button>
        ))}
      </div>
      <Suspense fallback={<div className="ws-tools-empty" role="status">{t('common.loading', 'Loading...')}</div>}>
        {activeView === 'global' ? <WorkspaceGlobalToolsView selectedTenantId={selectedTenantId} /> : null}
        {activeView === 'mcp-servers' ? <WorkspaceMcpServersView selectedTenantId={selectedTenantId} /> : null}
        {activeView === 'custom-api' ? <WorkspaceCustomApiView selectedTenantId={selectedTenantId} /> : null}
        {activeView === 'agent-installed' ? <WorkspaceAgentInstalledToolsView selectedTenantId={selectedTenantId} /> : null}
      </Suspense>
    </div>
  );
}
