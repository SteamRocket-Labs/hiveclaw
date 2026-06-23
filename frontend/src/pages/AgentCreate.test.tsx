import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string) => fallback ?? key,
  }),
}));

vi.mock('react-router-dom', () => ({
  useNavigate: () => vi.fn(),
}));

vi.mock('../api/domains/agents', () => ({
  agentApi: {
    getHrAgent: vi.fn(),
  },
}));

import AgentCreate from './AgentCreate';

describe('AgentCreate HR-only creation path', () => {
  it('exposes only the HR Agent creation path', () => {
    const markup = renderToStaticMarkup(<AgentCreate />);

    expect(markup).toContain('Create digital employee');
    expect(markup).toContain('HR Agent');
    expect(markup).toContain('Capability governance');
    expect(markup).toContain('Use HR Agent for guided creation');
    expect(markup).not.toContain('Creation method');
    expect(markup).not.toContain('Blank employee');
    expect(markup).not.toContain('Company template');
    expect(markup).not.toContain('Natural language assistant');
    expect(markup).not.toContain('Employee identity');
    expect(markup).not.toContain('Create employee');
  });
});
