import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { toolsApi } from '../../api/domains/tools';
import { requestAppConfirm } from '../../components/AppDialogs';
import type { WorkspaceToolsViewProps } from './workspaceToolsModel';

export default function WorkspaceAgentInstalledToolsView({ selectedTenantId }: WorkspaceToolsViewProps) {
  const { t } = useTranslation();
  const [rows, setRows] = useState<any[]>([]);
  const requestVersion = useRef(0);

  const load = useCallback(async () => {
    const version = ++requestVersion.current;
    try {
      const data = await toolsApi.listAgentInstalled(selectedTenantId || undefined);
      if (version === requestVersion.current) setRows(data);
    } catch {
      if (version === requestVersion.current) setRows([]);
    }
  }, [selectedTenantId]);

  useEffect(() => {
    void load();
    return () => { requestVersion.current += 1; };
  }, [load]);

  return (
    <div>
      <p className="ws-tools-hint">
        {t('enterprise.tools.agentInstalledHint', 'These tools are installed directly by agents.')}
      </p>
      {rows.length === 0 ? (
        <div className="ws-tools-empty">{t('enterprise.tools.noAgentInstalledTools', 'No agent-installed tools')}</div>
      ) : (
        <div className="ws-tools-list-sm">
          {rows.map((row) => (
            <div key={row.agent_tool_id} className="card ws-tools-installed-card">
              <div className="ws-tools-cell-grow">
                <div className="ws-tools-row-8">
                  <span className="ws-tools-name">🔌 {row.tool_display_name}</span>
                  {row.mcp_server_name ? <span className="ws-tools-tag ws-tools-tag-mcp">MCP</span> : null}
                </div>
                <div className="ws-tools-sub">
                  🤖 {row.installed_by_agent_name || 'Unknown Agent'}
                  {row.installed_at ? <span> · {new Date(row.installed_at).toLocaleString()}</span> : null}
                </div>
              </div>
              <button className="btn btn-ghost ws-tools-danger-text" onClick={async () => {
                const confirmed = await requestAppConfirm({
                  title: t('common.delete', 'Delete'),
                  message: t('enterprise.tools.removeFromAgent', { name: row.tool_display_name }),
                  confirmLabel: t('common.delete', 'Delete'),
                  danger: true,
                });
                if (!confirmed) return;
                try { await toolsApi.removeAgentTool(row.agent_tool_id); } catch { /* idempotent refresh */ }
                await load();
              }}>
                🗑️ {t('enterprise.tools.delete', 'Delete')}
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
