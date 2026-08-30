import { useTranslation } from 'react-i18next';

import type { SessionWorkbench } from '../../api/domains/ccParity';

export function sessionToolEffectRecoveryModel(
  workbench: SessionWorkbench | null | undefined,
): { blocked: boolean } {
  const blocked = Array.isArray(workbench?.runtime_tasks) && workbench.runtime_tasks.some((task) => {
    const blocker = task && typeof task === 'object'
      ? (task as Record<string, unknown>).user_blocker
      : null;
    return Boolean(
      blocker
      && typeof blocker === 'object'
      && (blocker as Record<string, unknown>).reason_code === 'tool_effect_outcome_unknown',
    );
  });
  return { blocked };
}

export function SessionToolEffectRecoveryBanner() {
  const { t } = useTranslation();

  return (
    <div
      role="alert"
      data-testid="tool-effect-reconciliation-blocker"
      style={{
        padding: '7px 16px',
        borderTop: '1px solid rgba(245,158,11,0.25)',
        background: 'rgba(245,158,11,0.08)',
        fontSize: '12px',
        color: 'rgb(180,100,0)',
      }}
    >
      {t(
        'sessionWorkbench.toolEffectReconciliation.blocker',
        'A tool may already have taken effect. An administrator must verify the evidence before this session can continue.',
      )}
    </div>
  );
}
