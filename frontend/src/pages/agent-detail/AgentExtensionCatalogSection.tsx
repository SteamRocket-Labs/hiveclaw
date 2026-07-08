import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';

import {
  extensionsApi,
  type ExternalExtensionActivationSummary,
  type ExternalExtensionCatalogEntry,
} from '../../api/domains/extensions';
import { showAppToast } from '../../components/AppDialogs';
import './AgentExtensionCatalogSection.css';

type AgentExtensionCatalogSectionProps = {
  agentId: string;
  canManage?: boolean;
};

function componentLabel(componentType: string): string {
  if (componentType === 'mcp_server') return 'MCP';
  if (componentType === 'subagent') return 'Sub-agent';
  if (componentType === 'skill') return 'Skill';
  if (componentType === 'slash_command') return 'Command';
  return componentType;
}

function runtimeDescription(entry: ExternalExtensionCatalogEntry): string {
  const runtimeProjection = entry.metadata?.runtime_projection;
  if (runtimeProjection && typeof runtimeProjection === 'object' && 'description' in runtimeProjection) {
    const description = (runtimeProjection as { description?: unknown }).description;
    if (typeof description === 'string' && description.trim()) return description;
  }
  return entry.source_uri || entry.qualified_name;
}

export default function AgentExtensionCatalogSection({ agentId, canManage = false }: AgentExtensionCatalogSectionProps) {
  const { t } = useTranslation();
  const [catalog, setCatalog] = useState<ExternalExtensionCatalogEntry[]>([]);
  const [activeSnapshotIds, setActiveSnapshotIds] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [busySnapshotId, setBusySnapshotId] = useState<string | null>(null);

  const loadCatalog = async () => {
    setLoading(true);
    try {
      const [catalogEntries, agentExtensions] = await Promise.all([
        extensionsApi.listAgentExternalExtensionCatalog(agentId),
        extensionsApi.getAgentExtensions(agentId),
      ]);
      setCatalog(catalogEntries);
      setActiveSnapshotIds(
        new Set(
          (agentExtensions.external_activations ?? [])
            .filter((activation: ExternalExtensionActivationSummary) => activation.status === 'active')
            .map((activation: ExternalExtensionActivationSummary) => activation.snapshot_id),
        ),
      );
    } catch (error) {
      console.error(error);
      showAppToast(t('agent.extensions.catalogLoadFailed', 'Failed to load extension catalog'), 'error');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadCatalog();
  }, [agentId]);

  const entriesByType = useMemo(() => {
    const grouped = new Map<string, ExternalExtensionCatalogEntry[]>();
    for (const entry of catalog) {
      const current = grouped.get(entry.component_type) ?? [];
      current.push(entry);
      grouped.set(entry.component_type, current);
    }
    return Array.from(grouped.entries());
  }, [catalog]);

  const activateEntry = async (entry: ExternalExtensionCatalogEntry) => {
    setBusySnapshotId(entry.snapshot_id);
    try {
      await extensionsApi.activateExternalExtension(agentId, entry.snapshot_id);
      setActiveSnapshotIds((previous) => new Set(previous).add(entry.snapshot_id));
      showAppToast(t('agent.extensions.catalogActivated', 'Extension activated'), 'success');
    } catch (error) {
      console.error(error);
      showAppToast(t('agent.extensions.catalogActivateFailed', 'Failed to activate extension'), 'error');
    } finally {
      setBusySnapshotId(null);
    }
  };

  const deactivateEntry = async (entry: ExternalExtensionCatalogEntry) => {
    setBusySnapshotId(entry.snapshot_id);
    try {
      await extensionsApi.deactivateExternalExtension(agentId, entry.snapshot_id);
      setActiveSnapshotIds((previous) => {
        const next = new Set(previous);
        next.delete(entry.snapshot_id);
        return next;
      });
      showAppToast(t('agent.extensions.catalogDeactivated', 'Extension deactivated'), 'success');
    } catch (error) {
      console.error(error);
      showAppToast(t('agent.extensions.catalogDeactivateFailed', 'Failed to deactivate extension'), 'error');
    } finally {
      setBusySnapshotId(null);
    }
  };

  if (loading) {
    return <div className="agent-extension-catalog-loading">{t('common.loading', 'Loading...')}</div>;
  }

  return (
    <section className="agent-extension-catalog" data-testid="agent-extension-catalog">
      {catalog.length === 0 ? (
        <div className="card agent-extension-catalog-empty">
          {t('agent.extensions.catalogEmpty', 'No approved optional extensions are available yet.')}
        </div>
      ) : (
        entriesByType.map(([componentType, entries]) => (
          <div key={componentType} className="agent-extension-catalog-group">
            <div className="agent-extension-catalog-group-title">
              {componentLabel(componentType)} ({entries.length})
            </div>
            <div className="agent-extension-catalog-grid">
              {entries.map((entry) => {
                const isActive = activeSnapshotIds.has(entry.snapshot_id);
                return (
                  <div key={entry.id} className="card agent-extension-catalog-card">
                    <div className="agent-extension-catalog-main">
                      <div className="agent-extension-catalog-name">{entry.component_name}</div>
                      <div className="agent-extension-catalog-desc">{runtimeDescription(entry)}</div>
                      <div className="agent-extension-catalog-meta">
                        <span>{entry.source_format || 'external'}</span>
                        <span>{entry.policy}</span>
                        <span>{entry.status}</span>
                      </div>
                    </div>
                    {canManage ? (
                      <button
                        type="button"
                        className={`btn ${isActive ? 'btn-secondary' : 'btn-primary'} agent-extension-catalog-action`}
                        disabled={busySnapshotId === entry.snapshot_id}
                        onClick={() => void (isActive ? deactivateEntry(entry) : activateEntry(entry))}
                      >
                        {busySnapshotId === entry.snapshot_id
                          ? t('common.saving', 'Saving...')
                          : isActive
                            ? t('agent.extensions.catalogDeactivate', 'Deactivate')
                            : t('agent.extensions.catalogInstall', 'Install')}
                      </button>
                    ) : (
                      <span className="agent-extension-catalog-readonly">
                        {isActive ? t('agent.extensions.catalogActive', 'Active') : t('agent.extensions.catalogAvailable', 'Available')}
                      </span>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        ))
      )}
    </section>
  );
}
