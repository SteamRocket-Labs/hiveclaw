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
        onSelectedModelChange={() => {}}
        onSave={() => {}}
      />,
    );

    expect(markup).toContain('Eval CI');
    expect(markup).toContain('Runtime Status');
    expect(markup).toContain('Live Eval Model');
    expect(markup).toContain('DeepSeek V4 Flash');
    expect(markup).not.toContain('HIVE_EVAL_TENANT_ID');
    expect(markup).not.toContain('Eval User');
    expect(markup).not.toContain('tenant_id');
  });
});
