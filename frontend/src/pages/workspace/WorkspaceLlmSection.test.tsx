import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import WorkspaceLlmSection, { buildProviderDefaultPatch } from './WorkspaceLlmSection';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string) => fallback ?? key.split('.').pop() ?? key,
  }),
}));

describe('WorkspaceLlmSection', () => {
  it('resets model fields to the selected provider recommendation', () => {
    const patch = buildProviderDefaultPatch(
      [
        {
          provider: 'anthropic',
          display_name: 'Anthropic',
          protocol: 'anthropic',
          default_base_url: 'https://api.anthropic.com',
          supports_tool_choice: false,
          default_max_tokens: 8192,
          recommended_models: [{ model: 'claude-sonnet-4-5', label: 'Claude Sonnet 4.5' }],
        },
        {
          provider: 'deepseek',
          display_name: 'DeepSeek',
          protocol: 'openai_compatible',
          default_base_url: 'https://api.deepseek.com',
          supports_tool_choice: true,
          default_max_tokens: 8192,
          max_input_tokens: 1000000,
          supported_reasoning_modes: ['provider_default', 'enabled', 'disabled'],
          supported_reasoning_efforts: ['high', 'max'],
          recommended_models: [{ model: 'deepseek-v4-flash', label: 'DeepSeek V4 Flash', supports_reasoning: true }],
        },
      ],
      'deepseek',
      {
        max_output_tokens: '8192',
        max_input_tokens: '200000',
      },
    );

    expect(patch).toMatchObject({
      provider: 'deepseek',
      model: 'deepseek-v4-flash',
      label: 'DeepSeek V4 Flash',
      base_url: 'https://api.deepseek.com',
      max_input_tokens: '1000000',
      reasoning_mode: 'provider_default',
      reasoning_effort: '',
    });
  });

  it('renders provider factual reasoning notes without main-model recommendations', () => {
    const markup = renderToStaticMarkup(
      <WorkspaceLlmSection
        models={[]}
        providerOptions={[
          {
            provider: 'deepseek',
            display_name: 'DeepSeek',
            protocol: 'openai_compatible',
            default_base_url: 'https://api.deepseek.com',
            supports_tool_choice: true,
            default_max_tokens: 8192,
            reasoning_strategy: 'deepseek_thinking',
            supported_reasoning_modes: ['provider_default', 'enabled', 'disabled'],
            supported_reasoning_efforts: ['high', 'max'],
            supports_reasoning_preservation: true,
            supports_tools_with_reasoning: true,
          },
        ]}
        showAddModel={true}
        editingModelId={null}
        modelForm={{
          provider: 'deepseek',
          model: 'deepseek-v4-flash',
          api_key: '',
          base_url: 'https://api.deepseek.com',
          label: 'DeepSeek V4 Flash',
          supports_vision: false,
          max_output_tokens: '8192',
          max_input_tokens: '1000000',
          temperature: '',
          reasoning_mode: 'provider_default',
          reasoning_effort: '',
          reasoning_budget_tokens: '',
          reasoning_display: '',
          preserve_reasoning: false,
          text_verbosity: '',
          provider_options: '',
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

    expect(markup).toContain('DeepSeek thinking mode keeps tool calling available');
    expect(markup).not.toContain('not recommended');
  });

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
          reasoning_mode: 'provider_default',
          reasoning_effort: '',
          reasoning_budget_tokens: '',
          reasoning_display: '',
          preserve_reasoning: false,
          text_verbosity: '',
          provider_options: '',
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
