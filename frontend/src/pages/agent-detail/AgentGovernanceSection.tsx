import React from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import { enterpriseApi, type CapabilityDefinition, type CapabilityPolicy } from '../../api/domains/enterprise';

type PolicyMode = 'auto' | 'approval' | 'deny';

interface AgentGovernanceSectionProps {
  agentId: string;
  canManage: boolean;
  surfaceId?: string;
}

function capabilityLabel(capability: string): string {
  return capability.replace(/[._:]+/g, ' ').replace(/\b\w/g, (char) => char.toUpperCase());
}

function policyMode(policy?: CapabilityPolicy | null): PolicyMode {
  if (!policy) return 'auto';
  if (!policy.allowed) return 'deny';
  return policy.requires_approval ? 'approval' : 'auto';
}

function policyForCapability(policies: CapabilityPolicy[], capability: string, agentId: string): CapabilityPolicy | null {
  return (
    policies.find((policy) => policy.capability === capability && policy.agent_id === agentId) ||
    policies.find((policy) => policy.capability === capability && !policy.agent_id) ||
    null
  );
}

export default function AgentGovernanceSection({ agentId, canManage, surfaceId = 'capability-policies' }: AgentGovernanceSectionProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [savingCapability, setSavingCapability] = React.useState<string | null>(null);
  const [error, setError] = React.useState('');

  const { data: definitions = [] } = useQuery<CapabilityDefinition[]>({
    queryKey: ['capability-definitions'],
    queryFn: () => enterpriseApi.listCapabilityDefinitions(),
  });
  const { data: policies = [] } = useQuery<CapabilityPolicy[]>({
    queryKey: ['capability-policies', agentId],
    queryFn: () => enterpriseApi.listCapabilityPolicies(agentId),
    enabled: Boolean(agentId),
  });

  const capabilityNames = React.useMemo(() => {
    const names = new Set<string>();
    for (const definition of definitions) names.add(definition.capability);
    for (const policy of policies) names.add(policy.capability);
    return Array.from(names).sort();
  }, [definitions, policies]);

  const updatePolicy = async (capability: string, mode: PolicyMode) => {
    if (!canManage) return;
    setSavingCapability(capability);
    setError('');
    try {
      await enterpriseApi.upsertCapabilityPolicy({
        capability,
        agent_id: agentId,
        allowed: mode !== 'deny',
        requires_approval: mode === 'approval',
        conditions: {},
      });
      await queryClient.invalidateQueries({ queryKey: ['capability-policies', agentId] });
    } catch (exc: any) {
      setError(exc?.message || t('agent.governance.saveFailed', 'Failed to save capability policy'));
    } finally {
      setSavingCapability(null);
    }
  };

  return (
    <div data-testid={surfaceId}>
      <div style={{ marginBottom: '16px' }}>
        <h3 style={{ marginBottom: '4px' }}>{t('agent.governance.title', 'Governance')}</h3>
        <p style={{ fontSize: '13px', color: 'var(--text-tertiary)' }}>
          {t('agent.governance.description', 'Company capability policies are L1 hard rules. They are evaluated before session permission prompts.')}
        </p>
      </div>
      {error && (
        <div style={{ color: 'var(--error)', fontSize: '12px', marginBottom: '10px' }}>
          {error}
        </div>
      )}
      <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '13px' }}>
          <thead>
            <tr style={{ background: 'var(--bg-secondary)', color: 'var(--text-secondary)' }}>
              <th style={{ textAlign: 'left', padding: '10px 12px', fontWeight: 600 }}>{t('agent.governance.capability', 'Capability')}</th>
              <th style={{ textAlign: 'left', padding: '10px 12px', fontWeight: 600 }}>{t('agent.governance.tools', 'Tools')}</th>
              <th style={{ textAlign: 'left', padding: '10px 12px', fontWeight: 600 }}>{t('agent.governance.policy', 'Policy')}</th>
            </tr>
          </thead>
          <tbody>
            {capabilityNames.length === 0 ? (
              <tr>
                <td colSpan={3} style={{ padding: '18px 12px', color: 'var(--text-tertiary)' }}>
                  {t('agent.governance.empty', 'No capability definitions available.')}
                </td>
              </tr>
            ) : (
              capabilityNames.map((capability) => {
                const definition = definitions.find((item) => item.capability === capability);
                const mode = policyMode(policyForCapability(policies, capability, agentId));
                return (
                  <tr key={capability} style={{ borderTop: '1px solid var(--border-subtle)' }}>
                    <td style={{ padding: '10px 12px', verticalAlign: 'top' }}>
                      <div style={{ fontWeight: 500 }}>{capabilityLabel(capability)}</div>
                      <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>{capability}</div>
                    </td>
                    <td style={{ padding: '10px 12px', color: 'var(--text-secondary)', verticalAlign: 'top' }}>
                      {definition?.tools?.length ? definition.tools.join(', ') : '-'}
                    </td>
                    <td style={{ padding: '10px 12px', verticalAlign: 'top' }}>
                      <select
                        className="input"
                        disabled={!canManage || savingCapability === capability}
                        value={mode}
                        onChange={(event) => updatePolicy(capability, event.target.value as PolicyMode)}
                        style={{ width: '180px', fontSize: '12px' }}
                      >
                        <option value="auto">{t('agent.governance.auto', 'Auto')}</option>
                        <option value="approval">{t('agent.governance.approval', 'Require approval')}</option>
                        <option value="deny">{t('agent.governance.deny', 'Deny')}</option>
                      </select>
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
