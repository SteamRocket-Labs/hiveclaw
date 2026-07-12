import { useState } from 'react';
import { useTranslation } from 'react-i18next';

import type { EnterpriseMcpMetadataTool } from '../../api/domains/extensions';

interface McpMetadataReviewPanelProps {
  serverName: string;
  tools: EnterpriseMcpMetadataTool[];
  busyTool: string | null;
  onReview: (
    tool: EnterpriseMcpMetadataTool,
    decision: 'approve' | 'reject',
    canonicalDescription: string,
  ) => void;
}

export function McpMetadataReviewPanel({
  serverName,
  tools,
  busyTool,
  onReview,
}: McpMetadataReviewPanelProps) {
  const { t } = useTranslation();
  const [drafts, setDrafts] = useState<Record<string, string>>({});

  return (
    <section className="ws-mcp-review" aria-label={`${serverName} metadata review`}>
      <div className="ws-mcp-review-heading">
        <strong>{t('enterprise.tools.mcpMetadataReview', 'Metadata review')}</strong>
        <span>{t('enterprise.tools.mcpMetadataAdminOnly', 'Administrator-only evidence')}</span>
      </div>
      {tools.length === 0 ? (
        <div className="ws-tools-empty">
          {t('enterprise.tools.noMcpMetadata', 'No MCP tool metadata found.')}
        </div>
      ) : (
        <div className="ws-mcp-review-list">
          {tools.map((tool) => {
            const draft = drafts[tool.tool_id] ?? tool.canonical_description;
            const busy = busyTool === tool.tool_id;
            return (
              <article key={tool.tool_id} className="ws-mcp-review-card">
                <div className="ws-tools-row-between">
                  <div>
                    <strong>{tool.display_name}</strong>
                    <div className="ws-tools-tiny-muted">{tool.tool_name}</div>
                  </div>
                  <span className={`ws-mcp-trust-badge ${tool.runtime_approved ? 'approved' : 'blocked'}`}>
                    {tool.runtime_approved
                      ? t('enterprise.tools.mcpRuntimeApproved', 'Runtime approved')
                      : t('enterprise.tools.mcpRuntimeBlocked', 'Runtime blocked')}
                  </span>
                </div>
                <div className="ws-mcp-review-meta">
                  <span>{tool.trust_status}</span>
                  <span>{tool.trust_tier}</span>
                  <code title={tool.metadata_fingerprint}>{tool.metadata_fingerprint.slice(0, 12)}</code>
                </div>
                {tool.risk_flags.length > 0 ? (
                  <div className="ws-mcp-risk-flags">
                    {tool.risk_flags.map((flag) => <span key={flag}>{flag}</span>)}
                  </div>
                ) : null}
                <label className="ws-mcp-canonical-field">
                  <span>{t('enterprise.tools.canonicalDescription', 'Canonical runtime description')}</span>
                  <textarea
                    rows={3}
                    maxLength={500}
                    value={draft}
                    onChange={(event) => setDrafts((current) => ({
                      ...current,
                      [tool.tool_id]: event.target.value,
                    }))}
                  />
                </label>
                <details className="ws-mcp-raw-evidence">
                  <summary>{t('enterprise.tools.rawRemoteEvidence', 'Raw remote evidence')}</summary>
                  <pre>{tool.raw_description}</pre>
                  <pre>{JSON.stringify(tool.raw_schema, null, 2)}</pre>
                </details>
                <div className="ws-mcp-review-actions">
                  <button
                    type="button"
                    className="btn btn-primary btn-sm"
                    disabled={busy || !draft.trim()}
                    onClick={() => onReview(tool, 'approve', draft)}
                  >
                    {busy ? t('common.saving', 'Saving...') : t('enterprise.tools.approveFingerprint', 'Approve fingerprint')}
                  </button>
                  <button
                    type="button"
                    className="btn btn-secondary btn-sm"
                    disabled={busy}
                    onClick={() => onReview(tool, 'reject', draft)}
                  >
                    {t('common.reject', 'Reject')}
                  </button>
                </div>
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}
