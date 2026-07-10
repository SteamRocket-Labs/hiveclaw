import { useState } from 'react';
import { useTranslation } from 'react-i18next';

import WorkspaceAIAssetsSection from './WorkspaceAIAssetsSection';
import WorkspaceCapabilityFactorsSection from './WorkspaceCapabilityFactorsSection';
import WorkspaceExtensionCatalogSection from './WorkspaceExtensionCatalogSection';
import WorkspaceSkillsSection from './WorkspaceSkillsSection';
import WorkspaceSubagentsSection from './WorkspaceSubagentsSection';
import WorkspaceToolsSection from './WorkspaceToolsSection';
import './WorkspaceExtensionsSection.css';

type WorkspaceExtensionsSectionProps = {
  selectedTenantId: string;
};

type WorkspaceExtensionSubview = 'assets' | 'catalog' | 'mcp' | 'skills' | 'subagents' | 'factors';

const WORKSPACE_EXTENSION_SUBVIEWS: Array<{
  id: WorkspaceExtensionSubview;
  labelKey: string;
  fallback: string;
}> = [
  { id: 'assets', labelKey: 'enterprise.extensions.tabs.assets', fallback: 'AI Assets' },
  { id: 'catalog', labelKey: 'enterprise.extensions.tabs.catalog', fallback: 'Catalog' },
  { id: 'mcp', labelKey: 'enterprise.extensions.tabs.mcp', fallback: 'MCP & Plugins' },
  { id: 'skills', labelKey: 'enterprise.extensions.tabs.skills', fallback: 'Skills' },
  { id: 'subagents', labelKey: 'enterprise.extensions.tabs.subagents', fallback: 'Sub-agents' },
  { id: 'factors', labelKey: 'enterprise.extensions.tabs.factors', fallback: 'Factor Intake' },
];

export default function WorkspaceExtensionsSection({ selectedTenantId }: WorkspaceExtensionsSectionProps) {
  const { t } = useTranslation();
  const [activeSubview, setActiveSubview] = useState<WorkspaceExtensionSubview>('assets');

  return (
    <section className="workspace-extensions-section" data-testid="workspace-extensions-section">
      <div className="workspace-extensions-header">
        <h3 className="workspace-extensions-title">
          {t('enterprise.extensions.title', 'Extension Catalog')}
        </h3>
        <div className="workspace-extensions-subnav" role="tablist" aria-label={t('enterprise.extensions.tabsLabel', 'Extension types')}>
          {WORKSPACE_EXTENSION_SUBVIEWS.map((subview) => (
            <button
              key={subview.id}
              type="button"
              role="tab"
              aria-selected={activeSubview === subview.id}
              className={`workspace-extensions-subtab${activeSubview === subview.id ? ' active' : ''}`}
              onClick={() => setActiveSubview(subview.id)}
            >
              {t(subview.labelKey, subview.fallback)}
            </button>
          ))}
        </div>
      </div>

      <div className="workspace-extensions-body">
        {activeSubview === 'assets' && (
          <div data-testid="workspace-extensions-assets-view">
            <WorkspaceAIAssetsSection selectedTenantId={selectedTenantId} />
          </div>
        )}
        {activeSubview === 'catalog' && (
          <div data-testid="workspace-extensions-catalog-view">
            <WorkspaceExtensionCatalogSection />
          </div>
        )}
        {activeSubview === 'mcp' && (
          <div data-testid="workspace-extensions-mcp-view">
            <WorkspaceToolsSection selectedTenantId={selectedTenantId} />
          </div>
        )}
        {activeSubview === 'skills' && (
          <div data-testid="workspace-extensions-skills-view">
            <WorkspaceSkillsSection />
          </div>
        )}
        {activeSubview === 'subagents' && (
          <div data-testid="workspace-extensions-subagents-view">
            <WorkspaceSubagentsSection />
          </div>
        )}
        {activeSubview === 'factors' && (
          <div data-testid="workspace-extensions-factors-view">
            <WorkspaceCapabilityFactorsSection />
          </div>
        )}
      </div>
    </section>
  );
}
