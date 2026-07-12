import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';

import {
  extensionsApi,
  type EnterpriseMcpMetadataTool,
  type McpServerRecord,
} from '../../api/domains/extensions';
import { McpMetadataReviewPanel } from './WorkspaceMcpMetadataReview';
import type { WorkspaceToolsViewProps } from './workspaceToolsModel';

const MCP_STATUS_COLORS: Record<string, string> = {
  connected: 'var(--success)',
  needs_auth: 'var(--warning)',
  expired: 'var(--warning)',
  failed: 'var(--error)',
  error: 'var(--error)',
  disabled: 'var(--text-tertiary)',
};

export default function WorkspaceMcpServersView({ selectedTenantId }: WorkspaceToolsViewProps) {
  const { t } = useTranslation();
  const [servers, setServers] = useState<McpServerRecord[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [openServerId, setOpenServerId] = useState<string | null>(null);
  const [metadataTools, setMetadataTools] = useState<EnterpriseMcpMetadataTool[]>([]);
  const [metadataLoading, setMetadataLoading] = useState(false);
  const [metadataError, setMetadataError] = useState<string | null>(null);
  const [busyTool, setBusyTool] = useState<string | null>(null);
  const requestVersion = useRef(0);
  const metadataVersion = useRef(0);

  const load = useCallback(async () => {
    const version = ++requestVersion.current;
    setLoaded(false);
    try {
      const data = await extensionsApi.listEnterpriseMcpServers();
      if (version === requestVersion.current) setServers(data);
    } catch {
      if (version === requestVersion.current) setServers([]);
    } finally {
      if (version === requestVersion.current) setLoaded(true);
    }
  }, [selectedTenantId]);

  useEffect(() => {
    void load();
    return () => {
      requestVersion.current += 1;
      metadataVersion.current += 1;
    };
  }, [load]);

  const toggleMetadata = useCallback(async (serverId: string) => {
    if (openServerId === serverId) {
      metadataVersion.current += 1;
      setOpenServerId(null);
      return;
    }
    const version = ++metadataVersion.current;
    setOpenServerId(serverId);
    setMetadataTools([]);
    setMetadataError(null);
    setMetadataLoading(true);
    try {
      const tools = await extensionsApi.listEnterpriseMcpServerTools(serverId);
      if (version === metadataVersion.current) setMetadataTools(tools);
    } catch (error) {
      if (version === metadataVersion.current) {
        setMetadataError(error instanceof Error ? error.message : 'Failed to load MCP metadata.');
      }
    } finally {
      if (version === metadataVersion.current) setMetadataLoading(false);
    }
  }, [openServerId]);

  const reviewMetadata = useCallback(async (
    tool: EnterpriseMcpMetadataTool,
    decision: 'approve' | 'reject',
    canonicalDescription: string,
  ) => {
    if (!openServerId) return;
    setBusyTool(tool.tool_id);
    setMetadataError(null);
    try {
      await extensionsApi.reviewEnterpriseMcpServerToolMetadata(openServerId, tool.tool_name, {
        decision,
        expected_fingerprint: tool.metadata_fingerprint,
        ...(decision === 'approve' ? { canonical_description: canonicalDescription.trim() } : {}),
      });
      setMetadataTools(await extensionsApi.listEnterpriseMcpServerTools(openServerId));
    } catch (error) {
      setMetadataError(error instanceof Error ? error.message : 'Failed to review MCP metadata.');
    } finally {
      setBusyTool(null);
    }
  }, [openServerId]);

  return (
    <div>
      <p className="ws-tools-hint">
        {t('enterprise.tools.mcpServersHint', 'External MCP integrations managed as server-level connectors. Each server may expose many tools internally.')}
      </p>
      {!loaded ? (
        <div className="ws-tools-empty">{t('common.loading', 'Loading...')}</div>
      ) : servers.length === 0 ? (
        <div className="ws-tools-empty">{t('enterprise.tools.noMcpServers', 'No MCP servers yet. Add one from Extensions & Add-ons.')}</div>
      ) : (
        <div className="ws-tools-list">
          {servers.map((server) => (
            <div key={server.id} className="card ws-tools-card-pad">
              <div className="ws-tools-row-between">
                <div className="ws-tools-row-10-min">
                  <span className="ws-tools-emoji">🔌</span>
                  <div className="ws-tools-min0">
                    <div className="ws-tools-row-6">
                      <span className="ws-tools-title-13">{server.name}</span>
                      <span className="ws-tools-tag ws-tools-tag-mcp">MCP</span>
                      <span className="ws-tools-tiny-muted">{server.transport}</span>
                    </div>
                    <div className="ws-tools-meta-row">
                      <span className="ws-tools-inline-4">
                        <span className="ws-tools-status-dot" style={{ background: MCP_STATUS_COLORS[server.status] || 'var(--text-tertiary)' }} />
                        {t(`agent.extensions.status.${server.status}`, server.status)}
                      </span>
                      <span>·</span>
                      <span>{t('enterprise.tools.authStatus', { status: t(`agent.extensions.authStatus.${server.auth_status}`, server.auth_status), defaultValue: 'auth: {{status}}' })}</span>
                      <span>·</span>
                      <span>{t('agent.extensions.toolCount', { count: server.tool_count, defaultValue: '{{count}} tools' })}</span>
                    </div>
                  </div>
                </div>
                <span className="ws-tools-meta-shrink">{t('enterprise.tools.usedByAgents', { count: server.agent_count, defaultValue: '{{count}} agents' })}</span>
              </div>
              {server.agents.length > 0 ? (
                <div className="ws-tools-agent-wrap">
                  {server.agents.map((agent) => (
                    <span key={agent.id} className={`ws-tools-agent-chip ${agent.enabled ? 'enabled' : 'disabled'}`}>{agent.name}</span>
                  ))}
                </div>
              ) : null}
              <div className="ws-mcp-server-actions">
                <button type="button" className="btn btn-secondary btn-sm" onClick={() => void toggleMetadata(server.id)}>
                  {openServerId === server.id
                    ? t('common.close', 'Close')
                    : t('enterprise.tools.reviewMcpMetadata', 'Review metadata')}
                </button>
              </div>
              {openServerId === server.id ? (
                metadataLoading ? (
                  <div className="ws-tools-empty">{t('common.loading', 'Loading...')}</div>
                ) : (
                  <>
                    {metadataError ? <div className="ws-mcp-review-error" role="alert">{metadataError}</div> : null}
                    <McpMetadataReviewPanel
                      serverName={server.name}
                      tools={metadataTools}
                      busyTool={busyTool}
                      onReview={(tool, decision, canonical) => void reviewMetadata(tool, decision, canonical)}
                    />
                  </>
                )
              ) : null}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
