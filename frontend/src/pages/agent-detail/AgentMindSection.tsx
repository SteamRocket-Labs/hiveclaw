import React from 'react';
import { useTranslation } from 'react-i18next';

import FileBrowser, { type FileBrowserApi } from '../../components/FileBrowser';
import { fileApi } from '../../api/domains/files';

type AgentMindSectionProps = {
  agentId: string;
  canEdit: boolean;
};

export default function AgentMindSection({ agentId, canEdit }: AgentMindSectionProps) {
  const { t } = useTranslation();

  const adapter: FileBrowserApi = {
    list: (path) => fileApi.list(agentId, path),
    read: (path) => fileApi.read(agentId, path),
    write: (path, content) => fileApi.write(agentId, path, content),
    delete: (path) => fileApi.delete(agentId, path),
    downloadUrl: (path) => fileApi.downloadUrl(agentId, path),
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '24px' }}>
      <div>
        <h3 style={{ marginBottom: '4px', display: 'flex', alignItems: 'center', gap: '8px' }}>🧬 {t('agent.soul.title')}</h3>
        <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '12px' }}>
          {t('agent.mind.soulDesc', 'Core identity, personality, and behavior boundaries.')}
        </p>
        <FileBrowser api={adapter} singleFile="soul.md" title="" features={{ edit: canEdit }} />
      </div>

      <div>
        <h3 style={{ marginBottom: '4px', display: 'flex', alignItems: 'center', gap: '8px' }}>🧠 {t('agent.memory.title')}</h3>
        <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '12px' }}>
          {t('agent.mind.memoryDesc', 'Long-term knowledge curated from conversations. Feedback, strategies, blocked patterns, and project knowledge.')}
        </p>
        <FileBrowser api={adapter} rootPath="memory" features={{ edit: canEdit }} />
      </div>

      <div>
        <h3 style={{ marginBottom: '4px', display: 'flex', alignItems: 'center', gap: '8px' }}>📓 {t('agent.mind.evolutionTitle', 'Evolution')}</h3>
        <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '12px' }}>
          {t('agent.mind.evolutionDesc', 'Curation history, performance scorecard, and blocked approaches.')}
        </p>
        <FileBrowser api={adapter} rootPath="evolution" readOnly features={{}} />
      </div>
    </div>
  );
}
