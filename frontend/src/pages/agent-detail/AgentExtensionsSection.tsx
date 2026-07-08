import { useState } from 'react';
import { useTranslation } from 'react-i18next';

import AgentCapabilityFactorsSection from './AgentCapabilityFactorsSection';
import AgentExtensionCatalogSection from './AgentExtensionCatalogSection';
import AgentSkillsSection from './AgentSkillsSection';
import AgentSubagentsSection from './AgentSubagentsSection';
import ToolsManager from './ToolsManager';
import './AgentExtensionsSection.css';

type AgentExtensionsSectionProps = {
  agentId: string;
  canManage?: boolean;
};

type ExtensionSubview = 'catalog' | 'mcp' | 'skills' | 'subagents' | 'factors';

const EXTENSION_SUBVIEWS: Array<{
  id: ExtensionSubview;
  labelKey: string;
  fallback: string;
}> = [
  { id: 'catalog', labelKey: 'agent.extensions.tabs.catalog', fallback: 'Catalog' },
  { id: 'mcp', labelKey: 'agent.extensions.tabs.mcp', fallback: 'MCP & Plugins' },
  { id: 'skills', labelKey: 'agent.extensions.tabs.skills', fallback: 'Skills' },
  { id: 'subagents', labelKey: 'agent.extensions.tabs.subagents', fallback: 'Sub-agents' },
  { id: 'factors', labelKey: 'agent.extensions.tabs.factors', fallback: 'Self-grown' },
];

export default function AgentExtensionsSection({ agentId, canManage = false }: AgentExtensionsSectionProps) {
  const { t } = useTranslation();
  const [activeSubview, setActiveSubview] = useState<ExtensionSubview>('catalog');

  return (
    <section className="agent-extensions-section" data-testid="agent-extensions-section">
      <div className="agent-extensions-header">
        <h3 className="agent-extensions-title">
          {t('agent.extensions.title', 'Capabilities')}
        </h3>
        <div className="agent-extensions-subnav" role="tablist" aria-label={t('agent.extensions.tabsLabel', 'Capability types')}>
          {EXTENSION_SUBVIEWS.map((subview) => (
            <button
              key={subview.id}
              type="button"
              role="tab"
              aria-selected={activeSubview === subview.id}
              className={`agent-extensions-subtab${activeSubview === subview.id ? ' active' : ''}`}
              onClick={() => setActiveSubview(subview.id)}
            >
              {t(subview.labelKey, subview.fallback)}
            </button>
          ))}
        </div>
      </div>

      <div className="agent-extensions-body">
        {activeSubview === 'catalog' && (
          <div data-testid="agent-extensions-catalog-view">
            <AgentExtensionCatalogSection agentId={agentId} canManage={canManage} />
          </div>
        )}
        {activeSubview === 'mcp' && (
          <div data-testid="agent-extensions-mcp-view">
            <ToolsManager agentId={agentId} canManage={canManage} />
          </div>
        )}
        {activeSubview === 'skills' && (
          <div data-testid="agent-extensions-skills-view">
            <AgentSkillsSection agentId={agentId} />
          </div>
        )}
        {activeSubview === 'subagents' && (
          <div data-testid="agent-extensions-subagents-view">
            <AgentSubagentsSection agentId={agentId} canManage={canManage} />
          </div>
        )}
        {activeSubview === 'factors' && (
          <div data-testid="agent-extensions-factors-view">
            <AgentCapabilityFactorsSection agentId={agentId} canManage={canManage} />
          </div>
        )}
      </div>
    </section>
  );
}
