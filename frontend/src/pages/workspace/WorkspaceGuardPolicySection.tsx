import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import {
  governanceApi,
  type GuardPolicy,
  type GuardPolicyDecision,
  type GuardPolicyLane,
  type GuardPolicyRule,
  type GuardPolicyUpdate,
} from '../../api/domains/governance';
import { showAppToast } from '../../components/AppDialogs';
import './WorkspaceGuardPolicySection.css';

const CONTROL_PLANE_RULE_ID = 'control_plane_default';

export type GuardrailPosture = 'inherit' | Extract<GuardPolicyDecision, 'require_approval' | 'deny'>;

export interface GuardrailControls {
  allActions: GuardrailPosture;
  externalActions: GuardrailPosture;
  additionalRuleCount: number;
}

interface WorkspaceGuardPolicySectionProps {
  initialPolicy?: GuardPolicy;
}

function rulesForLane(lane: GuardPolicyLane | null | undefined): GuardPolicyRule[] {
  return Array.isArray(lane?.tool_rules) ? lane.tool_rules : [];
}

function isControlPlaneRule(rule: GuardPolicyRule): boolean {
  return rule.rule_id === CONTROL_PLANE_RULE_ID;
}

function postureForLane(lane: GuardPolicyLane | null | undefined): GuardrailPosture {
  const rule = rulesForLane(lane).find(isControlPlaneRule);
  return rule?.decision === 'require_approval' || rule?.decision === 'deny'
    ? rule.decision
    : 'inherit';
}

export function readGuardrailControls(policy: GuardPolicy): GuardrailControls {
  const additionalRuleCount = [
    ...rulesForLane(policy.zone_guard),
    ...rulesForLane(policy.egress_guard),
  ].filter((rule) => !isControlPlaneRule(rule)).length;
  return {
    allActions: postureForLane(policy.zone_guard),
    externalActions: postureForLane(policy.egress_guard),
    additionalRuleCount,
  };
}

function buildLane(
  lane: GuardPolicyLane,
  posture: GuardrailPosture,
  reason: string,
): GuardPolicyLane {
  const nextRules = rulesForLane(lane).filter((rule) => !isControlPlaneRule(rule));
  if (posture !== 'inherit') {
    nextRules.push({
      rule_id: CONTROL_PLANE_RULE_ID,
      tools: ['*'],
      decision: posture,
      reason,
    });
  }
  return {
    ...lane,
    tool_rules: nextRules,
  };
}

export function buildGuardPolicyUpdate(
  policy: GuardPolicy,
  controls: Pick<GuardrailControls, 'allActions' | 'externalActions'>,
): GuardPolicyUpdate & { zone_guard: GuardPolicyLane; egress_guard: GuardPolicyLane } {
  return {
    expected_version: policy.version,
    zone_guard: buildLane(
      policy.zone_guard,
      controls.allActions,
      'Company approval is required for every action',
    ),
    egress_guard: buildLane(
      policy.egress_guard,
      controls.externalActions,
      'Company approval is required for outbound actions',
    ),
  };
}

function GuardrailEditor({ policy }: { policy: GuardPolicy }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const initialControls = readGuardrailControls(policy);
  const [allActions, setAllActions] = useState<GuardrailPosture>(initialControls.allActions);
  const [externalActions, setExternalActions] = useState<GuardrailPosture>(initialControls.externalActions);

  const mutation = useMutation({
    mutationFn: () => governanceApi.updateGuardPolicy(
      buildGuardPolicyUpdate(policy, { allActions, externalActions }),
    ),
    onSuccess: (nextPolicy) => {
      queryClient.setQueryData(['guard-policy', nextPolicy.tenant_id], nextPolicy);
      showAppToast(t('guardrails.saved', 'Action guardrails saved'), 'success');
    },
    onError: (error: Error) => {
      showAppToast(
        error.message || t('guardrails.saveFailed', 'Could not save action guardrails'),
        'error',
      );
    },
  });

  const dirty = allActions !== initialControls.allActions
    || externalActions !== initialControls.externalActions;
  const options: Array<{ value: GuardrailPosture; label: string; description: string }> = [
    {
      value: 'inherit',
      label: t('guardrails.postures.inherit', 'Follow normal permissions'),
      description: t(
        'guardrails.postures.inheritDesc',
        'Use each employee’s permissions, approval settings, and runtime protections.',
      ),
    },
    {
      value: 'require_approval',
      label: t('guardrails.postures.approval', 'Require approval'),
      description: t(
        'guardrails.postures.approvalDesc',
        'Pause matching actions until an authorized reviewer approves them.',
      ),
    },
    {
      value: 'deny',
      label: t('guardrails.postures.block', 'Block'),
      description: t(
        'guardrails.postures.blockDesc',
        'Prevent matching actions even when an employee would otherwise have permission.',
      ),
    },
  ];

  const renderControl = (
    id: string,
    title: string,
    description: string,
    value: GuardrailPosture,
    onChange: (value: GuardrailPosture) => void,
  ) => (
    <label className="guardrail-control" htmlFor={id}>
      <span>
        <strong>{title}</strong>
        <small>{description}</small>
      </span>
      <select
        id={id}
        className="form-input guardrail-select"
        value={value}
        onChange={(event) => onChange(event.target.value as GuardrailPosture)}
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
      <small className="guardrail-posture-description">
        {options.find((option) => option.value === value)?.description}
      </small>
    </label>
  );

  return (
    <>
      <div className="guardrail-controls">
        {renderControl(
          'guardrail-all-actions',
          t('guardrails.allActions', 'Every employee action'),
          t(
            'guardrails.allActionsDesc',
            'A company-wide backstop applied after normal employee permissions.',
          ),
          allActions,
          setAllActions,
        )}
        {renderControl(
          'guardrail-external-actions',
          t('guardrails.externalActions', 'Actions that leave the company'),
          t(
            'guardrails.externalActionsDesc',
            'Messages, files, commands, and other actions visible outside Hive.',
          ),
          externalActions,
          setExternalActions,
        )}
      </div>

      {initialControls.additionalRuleCount > 0 ? (
        <div className="guardrail-managed-note" role="status">
          {initialControls.additionalRuleCount === 1
            ? t('guardrails.oneAdditionalRule', '1 additional managed rule')
            : `${initialControls.additionalRuleCount} ${t(
              'guardrails.additionalRules',
              'additional managed rules',
            )}`}
          {' · '}
          {t(
            'guardrails.additionalRulesDesc',
            'Saving here preserves rules installed by platform extensions.',
          )}
        </div>
      ) : null}

      <div className="guardrail-save-row">
        <span>
          {t('guardrails.version', 'Policy version {{version}}', { version: policy.version })}
        </span>
        <button
          type="button"
          className="btn btn-primary"
          disabled={!dirty || mutation.isPending}
          onClick={() => mutation.mutate()}
        >
          {mutation.isPending
            ? t('common.saving', 'Saving…')
            : t('common.save', 'Save')}
        </button>
      </div>
    </>
  );
}

export default function WorkspaceGuardPolicySection({
  initialPolicy,
}: WorkspaceGuardPolicySectionProps) {
  const { t } = useTranslation();
  const selectedTenantId = (() => {
    try {
      return localStorage.getItem('current_tenant_id') || '';
    } catch {
      return '';
    }
  })();
  const {
    data: policy,
    error,
    isLoading,
    refetch,
  } = useQuery({
    queryKey: ['guard-policy', selectedTenantId || initialPolicy?.tenant_id || 'current'],
    queryFn: governanceApi.getGuardPolicy,
    initialData: initialPolicy,
  });

  return (
    <section className="workspace-guardrails" data-testid="workspace-guard-policy-section">
      <div className="workspace-guardrails-header">
        <div>
          <span>{t('guardrails.eyebrow', 'Company action policy')}</span>
          <h2>{t('guardrails.title', 'Action Guardrails')}</h2>
          <p>
            {t(
              'guardrails.description',
              'Set business-level backstops for every digital employee. These controls only narrow existing authority; they never grant new access.',
            )}
          </p>
        </div>
      </div>

      {isLoading ? (
        <div className="empty-state">{t('common.loading', 'Loading…')}</div>
      ) : error || !policy ? (
        <div className="empty-state">
          <p>{t('guardrails.loadFailed', 'Could not load company action guardrails.')}</p>
          <button type="button" className="btn btn-secondary" onClick={() => void refetch()}>
            {t('common.retry', 'Retry')}
          </button>
        </div>
      ) : (
        <GuardrailEditor key={`${policy.id}:${policy.version}`} policy={policy} />
      )}
    </section>
  );
}
