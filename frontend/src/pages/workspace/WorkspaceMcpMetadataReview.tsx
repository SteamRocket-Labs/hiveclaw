import { useState } from 'react';
import { useTranslation } from 'react-i18next';

import type { EnterpriseMcpActivationFacts, EnterpriseMcpMetadataTool } from '../../api/domains/extensions';

function configurationText(
  configuration: NonNullable<EnterpriseMcpActivationFacts['configuration']>,
): string {
  const parts: string[] = [];
  if (configuration.transport) parts.push(configuration.transport);
  if (configuration.command) parts.push(configuration.command);
  if (Array.isArray(configuration.args)) parts.push(configuration.args.join(' '));
  else if (configuration.args) parts.push(configuration.args);
  if (Array.isArray(configuration.scopes)) parts.push(configuration.scopes.join(', '));
  else if (configuration.scopes) parts.push(configuration.scopes);
  if (configuration.headers && typeof configuration.headers === 'object') {
    parts.push(Object.entries(configuration.headers).map(([name, value]) => `${name} ${value}`).join(', '));
  } else if (typeof configuration.headers === 'string') {
    parts.push(configuration.headers);
  }
  return parts.filter(Boolean).join(' · ');
}

function ActivationFacts({ tool }: { tool: EnterpriseMcpMetadataTool }) {
  const { t } = useTranslation();
  const activation = tool.activation;
  if (!activation) return null;
  const configText = activation.configuration ? configurationText(activation.configuration) : '';
  const previous = tool.reviewed_fingerprint
    && tool.reviewed_fingerprint !== tool.metadata_fingerprint
    ? tool.reviewed_fingerprint
    : null;
  const credentialSet = activation.configuration?.credential === 'set';
  if (
    !activation.endpoint
    && !activation.package
    && !activation.permissions?.length
    && !configText
    && !credentialSet
    && !previous
  ) {
    return null;
  }
  return (
    <dl className="ws-mcp-activation" aria-label={t('enterprise.tools.mcpActivationFacts', 'Activation being approved')}>
      {activation.endpoint ? (
        <div>
          <dt>{t('enterprise.tools.mcpActivationEndpoint', 'Endpoint')}</dt>
          <dd>{activation.endpoint}</dd>
        </div>
      ) : null}
      {activation.package ? (
        <div>
          <dt>{t('enterprise.tools.mcpActivationPackage', 'Package')}</dt>
          <dd>{activation.package}</dd>
        </div>
      ) : null}
      {activation.permissions?.length ? (
        <div>
          <dt>{t('enterprise.tools.mcpActivationPermissions', 'Permissions')}</dt>
          <dd>{activation.permissions.join(', ')}</dd>
        </div>
      ) : null}
      {configText ? (
        <div>
          <dt>{t('enterprise.tools.mcpActivationConfiguration', 'Configuration')}</dt>
          <dd>{configText}</dd>
        </div>
      ) : null}
      {credentialSet ? (
        <div>
          <dt>{t('enterprise.tools.mcpActivationCredential', 'Credential')}</dt>
          <dd>{t('enterprise.tools.mcpActivationCredentialSet', 'Set')}</dd>
        </div>
      ) : null}
      {previous ? (
        <div>
          <dt>{t('enterprise.tools.mcpPreviouslyApproved', 'Previously approved')}</dt>
          <dd><code title={previous}>{previous.slice(0, 12)}</code></dd>
        </div>
      ) : null}
    </dl>
  );
}

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
                <ActivationFacts tool={tool} />
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
