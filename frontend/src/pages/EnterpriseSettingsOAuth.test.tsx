import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import EnterpriseSettings from './EnterpriseSettings';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallbackOrOptions?: string | Record<string, unknown>) =>
      typeof fallbackOrOptions === 'string' ? fallbackOrOptions : key.split('.').pop() ?? key,
  }),
}));

vi.mock('@tanstack/react-query', () => ({
  useQuery: ({ queryKey }: { queryKey: string[] }) => {
    const key = queryKey[0];
    if (key === 'llm-models') {
      return {
        data: [
          {
            id: 'm1',
            provider: 'openai',
            model: 'gpt-5.4',
            label: 'GPT-5.4',
            enabled: true,
            temperature: 1,
            discovered_from_provider: true,
            provider_config_id: 'pc-1',
          },
        ],
      };
    }
    if (key === 'llm-provider-specs') {
      return {
        data: [
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
          },
        ],
      };
    }
    if (key === 'llm-provider-configs') {
      return {
        data: [
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
        ],
      };
    }
    return { data: [] };
  },
  useMutation: () => ({
    mutate: vi.fn(),
    mutateAsync: vi.fn(async () => ({})),
  }),
  useQueryClient: () => ({
    invalidateQueries: vi.fn(),
  }),
}));

vi.mock('react-router-dom', () => ({
  useNavigate: () => vi.fn(),
}));

vi.mock('../api/domains/auth', () => ({
  authApi: {
    getMe: vi.fn(async () => null),
  },
}));

vi.mock('../api/domains/enterprise', () => ({
  enterpriseApi: {
    llmModels: vi.fn(async () => []),
    getLLMProviders: vi.fn(async () => []),
    getLLMProviderConfigs: vi.fn(async () => []),
    createLLMModel: vi.fn(),
    updateLLMModel: vi.fn(),
    deleteLLMModel: vi.fn(),
    testLLM: vi.fn(),
    setDefaultModel: vi.fn(),
    saveLLMProviderConfig: vi.fn(),
    verifyLLMProviderConfig: vi.fn(),
    refreshLLMProviderModels: vi.fn(),
    startLLMProviderAuth: vi.fn(),
    getLLMProviderAuthStatus: vi.fn(),
    disconnectLLMProviderAuth: vi.fn(),
    getStats: vi.fn(async () => ({ total_users: 0, running_agents: 0, total_agents: 0, pending_approvals: 0 })),
    getInfo: vi.fn(async () => []),
    getSetting: vi.fn(async () => ({ value: {} })),
    updateSetting: vi.fn(async () => ({})),
    kbFiles: vi.fn(async () => []),
    kbRead: vi.fn(async () => ({})),
    kbWrite: vi.fn(async () => ({})),
    kbDelete: vi.fn(async () => ({})),
    kbUpload: vi.fn(async () => ({})),
  },
}));

vi.mock('../api/domains/notifications', () => ({
  notificationsApi: {},
}));

vi.mock('../api/domains/system', () => ({
  systemApi: {
    getTenant: vi.fn(async () => ({ name: 'Test Company', timezone: 'Asia/Shanghai' })),
    updateTenant: vi.fn(async () => ({})),
    deleteTenant: vi.fn(async () => ({ needs_company_setup: false })),
  },
}));

vi.mock('../stores', () => ({
  useAuthStore: () => ({
    user: { id: 'u1', role: 'platform_admin' },
    setUser: vi.fn(),
  }),
}));

vi.mock('../components/FileBrowser', () => ({
  default: () => <div>FileBrowser</div>,
}));

vi.mock('./workspace/WorkspaceApprovalsSection', () => ({ default: () => <div>Approvals</div> }));
vi.mock('./workspace/WorkspaceAuditSection', () => ({ default: () => <div>Audit</div> }));
vi.mock('./workspace/WorkspaceInfoSection', () => ({ default: () => <div>Info</div> }));
vi.mock('./workspace/WorkspaceInvitesSection', () => ({ default: () => <div>Invites</div> }));
vi.mock('./workspace/WorkspaceOrgSection', () => ({ default: () => <div>Org</div> }));
vi.mock('./workspace/WorkspaceQuotasSection', () => ({ default: () => <div>Quotas</div> }));
vi.mock('./workspace/WorkspaceSkillsSection', () => ({ default: () => <div>Skills</div> }));
vi.mock('./workspace/WorkspaceHrAgentSection', () => ({ default: () => <div>HR</div> }));
vi.mock('./workspace/WorkspaceMemorySection', () => ({ default: () => <div>Memory</div> }));
vi.mock('./workspace/WorkspaceToolsSection', () => ({ default: () => <div>Tools</div> }));
vi.mock('./workspace/WorkspaceUsersSection', () => ({ default: () => <div>Users</div> }));

describe('EnterpriseSettings OAuth section', () => {
  it('renders provider login actions inside the enterprise llm tab', () => {
    const markup = renderToStaticMarkup(<EnterpriseSettings forcedTab="llm" hideTabs />);

    expect(markup).toContain('OpenAI');
    expect(markup).toContain('使用 ChatGPT 登录');
    expect(markup).toContain('GPT-5.4');
  });
});
