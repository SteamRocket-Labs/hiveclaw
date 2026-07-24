import React from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import FileBrowser, { type FileBrowserApi } from '../../components/FileBrowser';
import { fileApi } from '../../api/domains/files';
import './AgentMindSection.css';

type AgentMindSectionProps = {
  agentId: string;
  canEdit: boolean;
};

export default function AgentMindSection({ agentId, canEdit }: AgentMindSectionProps) {
  const { t } = useTranslation();
  const soulQuery = useQuery({
    queryKey: ['agent-soul', agentId],
    queryFn: () => fileApi.read(agentId, 'soul.md'),
    enabled: canEdit && !!agentId,
  });

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
        <div className="agent-mind-soul-heading">
          <h3>🧬 {t('agent.mind.currentIdentityTitle', 'Current identity')}</h3>
          <span className="badge">{t('agent.mind.readOnly', 'Read only')}</span>
        </div>
        <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '12px' }}>
          {t(
            'agent.mind.currentIdentityDesc',
            'The complete identity and behavior contract this employee receives at the start of every conversation.',
          )}
        </p>
        <div className="agent-mind-governed-notice">
          {t('agent.mind.soulGovernedNotice', 'soul.md is governed by Dream/Soul promotion.')}
        </div>
        <div className="card agent-mind-soul-card">
          <div className="agent-mind-soul-source">
            <span>{t('agent.mind.fullIdentity', 'Full identity')}</span>
            <code>soul.md</code>
          </div>
          {soulQuery.isLoading ? (
            <div className="agent-mind-soul-state">
              {t('agent.mind.identityLoading', 'Loading current identity…')}
            </div>
          ) : soulQuery.isError ? (
            <div className="agent-mind-soul-state is-error" role="alert">
              <span>
                {t('agent.mind.identityLoadFailed', 'Current identity could not be loaded.')}
              </span>
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                onClick={() => soulQuery.refetch()}
              >
                {t('common.retry')}
              </button>
            </div>
          ) : soulQuery.data?.content ? (
            <details className="agent-mind-soul-disclosure" open>
              <summary>{t('agent.mind.fullIdentity', 'Full identity')}</summary>
              <pre className="agent-mind-soul-content">{soulQuery.data.content}</pre>
            </details>
          ) : (
            <div className="agent-mind-soul-state">
              {t('agent.mind.identityEmpty', 'No identity content has been created yet.')}
            </div>
          )}
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
