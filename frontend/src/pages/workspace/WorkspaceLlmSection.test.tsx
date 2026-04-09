import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import WorkspaceLlmSection from './WorkspaceLlmSection';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string) => fallback ?? key.split('.').pop() ?? key,
  }),
}));

describe('WorkspaceLlmSection', () => {
  it('renders the model pool controls as a standalone workspace page section', () => {
    const markup = renderToStaticMarkup(
      <WorkspaceLlmSection
        models={[
          {
            id: 'm1',
            provider: 'anthropic',
            model: 'claude-sonnet',
            label: 'Claude Sonnet',
            enabled: true,
            supports_vision: true,
            base_url: 'https://api.anthropic.com',
          },
        ]}
        providerOptions={[
          {
            provider: 'anthropic',
            display_name: 'Anthropic',
            protocol: 'anthropic',
            default_base_url: 'https://api.anthropic.com',
            supports_tool_choice: false,
            default_max_tokens: 8192,
          },
        ]}
        showAddModel={false}
        editingModelId={null}
        modelForm={{
          provider: 'anthropic',
          model: '',
          api_key: '',
          base_url: '',
          label: '',
          supports_vision: false,
          max_output_tokens: '',
          max_input_tokens: '',
          temperature: '',
        }}
        onStartCreateModel={() => {}}
        onCancelModelForm={() => {}}
        onModelFormChange={() => {}}
        onTestDraftModel={() => {}}
        onCreateModel={() => {}}
        onTestExistingModel={() => {}}
        onUpdateModel={() => {}}
        onToggleModel={() => {}}
        onEditModel={() => {}}
        onDeleteModel={() => {}}
      />,
    );

    expect(markup).toContain('Add Model');
    expect(markup).toContain('Claude Sonnet');
    expect(markup).toContain('Vision');
  });
});
