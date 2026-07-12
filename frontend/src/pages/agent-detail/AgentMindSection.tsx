import React from 'react';
import { useTranslation } from 'react-i18next';

import FileBrowser, { type FileBrowserApi } from '../../components/FileBrowser';
import { fileApi } from '../../api/domains/files';

type AgentMindSectionProps = {
  agentId: string;
  canEdit: boolean;
};

export default function AgentMindSection({ agentId }: AgentMindSectionProps) {
  const { t } = useTranslation();

  const adapter = React.useMemo<FileBrowserApi>(() => ({
    list: (path) => fileApi.list(agentId, path),
    read: (path) => fileApi.read(agentId, path),
    write: (path, content) => fileApi.write(agentId, path, content),
    delete: (path) => fileApi.delete(agentId, path),
    download: (path) => fileApi.download(agentId, path),
  }), [agentId]);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '24px' }}>
      <div>
        <h3 style={{ marginBottom: '4px', display: 'flex', alignItems: 'center', gap: '8px' }}>🧬 {t('agent.soul.title')}</h3>
        <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '12px' }}>
          {t('agent.mind.soulDesc', 'Core identity, personality, and behavior boundaries.')}
        </p>
        <div
          style={{
            padding: '12px 14px',
            borderRadius: '8px',
            background: 'var(--bg-secondary)',
            color: 'var(--text-secondary)',
            fontSize: '13px',
            lineHeight: 1.5,
          }}
        >
          {t('agent.mind.soulGovernedNotice', 'soul.md is governed by Dream/Soul promotion.')}
        </div>
      </div>

      <div>
        <h3 style={{ marginBottom: '4px', display: 'flex', alignItems: 'center', gap: '8px' }}>🧠 {t('agent.memory.title')}</h3>
        <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '12px' }}>
          {t('agent.mind.memoryDesc', 'Long-term knowledge curated from conversations. Feedback, strategies, blocked patterns, and project knowledge.')}
        </p>
        <FileBrowser api={adapter} rootPath="memory" readOnly features={{}} />
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
