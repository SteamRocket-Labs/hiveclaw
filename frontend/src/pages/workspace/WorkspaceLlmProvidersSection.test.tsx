import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import WorkspaceLlmProvidersSection from './WorkspaceLlmProvidersSection';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallbackOrOptions?: string | Record<string, unknown>) =>
      typeof fallbackOrOptions === 'string' ? fallbackOrOptions : key.split('.').pop() ?? key,
  }),
}));

describe('WorkspaceLlmProvidersSection', () => {
  it('renders provider-first layout with auth actions and model list', () => {
    const markup = renderToStaticMarkup(
      <WorkspaceLlmProvidersSection
        selectedProvider="openai"
        providerConfigs={[
          {
            provider: 'openai',
            display_name: 'OpenAI',
            protocol: 'openai_compatible',
            default_base_url: 'https://api.openai.com/v1',
            supports_tool_choice: true,
            default_max_tokens: 16384,
            supported_auth_modes: ['api_key', 'login_bridge'],
            supports_model_discovery: false,
            managed_login_label: '使用 ChatGPT 登录',
            known_models: ['gpt-5.4'],
            source: 'saved',
            auth_mode: 'login_bridge',
            enabled: true,
            base_url: 'https://api.openai.com/v1',
            api_key_masked: '****1234',
            connected: true,
            model_count: 1,
            models: [],
          },
          {
            provider: 'minimax',
            display_name: 'MiniMax',
            protocol: 'openai_compatible',
            default_base_url: 'https://api.minimaxi.com/v1',
            supports_tool_choice: true,
            default_max_tokens: 16384,
            supported_auth_modes: ['api_key', 'oauth_web', 'oauth_device'],
            supports_model_discovery: true,
            managed_login_label: null,
            known_models: [],
            source: 'empty',
            auth_mode: 'api_key',
            enabled: true,
            base_url: 'https://api.minimaxi.com/v1',
            api_key_masked: '',
            connected: false,
            model_count: 0,
            models: [],
          },
        ]}
        models={[
          {
            id: 'm1',
            provider: 'openai',
            model: 'gpt-5.4',
            label: 'GPT-5.4',
            enabled: true,
            supports_vision: false,
            discovered_from_provider: true,
            provider_config_id: 'pc-1',
            temperature: 1,
          },
        ]}
        configForm={{
          auth_mode: 'login_bridge',
          api_key: '',
          base_url: 'https://api.openai.com/v1',
          enabled: true,
        }}
        authSession={{
          session_id: 'session-1',
          mode: 'login_bridge',
          status: 'pending',
          connect_url: 'https://login.example.com',
          qr_url: null,
          user_code: null,
          expires_at: '2026-04-10T10:00:00Z',
          provider_payload: {},
        }}
        legacyEditor={null}
        onSelectProvider={() => {}}
        onConfigFormChange={() => {}}
        onSaveConfig={() => {}}
        onVerifyConfig={() => {}}
        onRefreshModels={() => {}}
        onStartAuth={() => {}}
        onDisconnectAuth={() => {}}
        onOpenManualCreate={() => {}}
        onEditModel={() => {}}
        onDeleteModel={() => {}}
        onToggleModel={() => {}}
        onSetDefaultModel={() => {}}
      />,
    );

    expect(markup).toContain('OpenAI');
    expect(markup).toContain('MiniMax');
    expect(markup).toContain('使用 ChatGPT 登录');
    expect(markup).toContain('Refresh');
    expect(markup).toContain('GPT-5.4');
  });
});
