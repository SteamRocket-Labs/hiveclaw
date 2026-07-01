import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import WorkspaceEvalCiSection from './WorkspaceEvalCiSection';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string) => fallback ?? key.split('.').pop() ?? key,
  }),
}));

describe('WorkspaceEvalCiSection', () => {
  it('renders eval CI as a standalone memory-style settings module', () => {
    const markup = renderToStaticMarkup(
      <WorkspaceEvalCiSection
        models={[
          {
            id: 'model-1',
            provider: 'deepseek',
            model: 'deepseek-v4-flash',
            label: 'DeepSeek V4 Flash',
            enabled: true,
            supports_vision: false,
          },
        ]}
        runtimeStatus={{
          configured: true,
          model: {
            provider: 'deepseek',
            model: 'deepseek-v4-flash',
            label: 'DeepSeek V4 Flash',
          },
          mirror: {
            source_model_id: 'model-1',
            provider: 'deepseek',
            model: 'deepseek-v4-flash',
            label: 'DeepSeek V4 Flash',
          },
        }}
        selectedModelId="model-1"
        saving={false}
        saved={false}
        latestReport={{
          available: true,
          stored_at: '2026-06-15T00:00:00+00:00',
          summary: {
            kind: 'behavior_eval',
            transport: 'hive_live',
            benchmark_complete: true,
            fallback_used: false,
            runtime: { model: 'deepseek-v4-flash' },
            scenarios: {
              coding: { ready: true, score: 100, transcript_chars: 1200 },
              research: { ready: false, score: 70, transcript_chars: 800 },
            },
          },
        }}
        latestReportLoading={false}
        runningBehaviorEval={false}
        onRunBehaviorEval={() => {}}
        onSelectedModelChange={() => {}}
        onSave={() => {}}
      />,
    );

    expect(markup).toContain('Behavior Evaluation');
    expect(markup).toContain('Runtime Status');
    expect(markup).toContain('Latest Behavior Report');
    expect(markup).toContain('Run Behavior Evaluation');
    expect(markup).toContain('coding');
    expect(markup).toContain('research');
    expect(markup).toContain('Live Eval Model');
    expect(markup).toContain('DeepSeek V4 Flash');
    expect(markup).not.toContain('HIVE_EVAL_TENANT_ID');
    expect(markup).not.toContain('Eval User');
    expect(markup).not.toContain('tenant_id');
  });
});
