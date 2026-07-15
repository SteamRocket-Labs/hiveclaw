import React from 'react';

import type { RuntimeBudgetState } from './runtimeBudgetState';

export function RuntimeBudgetNotice({ runtimeBudget }: { runtimeBudget?: RuntimeBudgetState | null }) {
  if (
    runtimeBudget?.status !== 'unavailable'
    || runtimeBudget.workAmplifyingToolsDisabled !== true
  ) {
    return null;
  }
  return (
    <div
      role="status"
      aria-live="polite"
      data-testid="runtime-budget-unavailable"
      style={{
        padding: '8px 16px',
        borderTop: '1px solid rgba(245,158,11,0.28)',
        background: 'rgba(245,158,11,0.09)',
        color: 'rgb(180,100,0)',
        fontSize: '12px',
        lineHeight: 1.5,
      }}
    >
      运行保护系统暂时不可用。当前回复仍会继续；子任务、工作流、跨 Agent 协作、目标续跑和自动唤醒已暂停。完成本轮后重试即可恢复。
    </div>
  );
}
