import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const read = (relativePath: string) => readFileSync(new URL(relativePath, import.meta.url), 'utf8');

describe('UX-04 orchestration and composition boundaries', () => {
  it('keeps AgentDetail as a page orchestrator while policy and transport have single owners', () => {
    const source = read('../AgentDetail.tsx');

    expect(source.split('\n').length).toBeLessThanOrEqual(2900);
    expect(source).toContain("from './agent-detail/agentDetailPolicy'");
    expect(source).toContain("from './agent-detail/useSessionTransportController'");
    expect(source).not.toContain('new WebSocket(');
    expect(source).not.toContain('reconnectTimersRef');
  });

  it('composes the chat surface from explicit lineage, artifact, runtime, and tool-result modules', () => {
    const source = read('./AgentChatSection.tsx');

    expect(source.split('\n').length).toBeLessThanOrEqual(2400);
    expect(source).toContain("from './SessionLineageSurface'");
    expect(source).toContain("from './ArtifactSurface'");
    expect(source).toContain("from './SessionRuntimePanel'");
    expect(source).toContain("from './StructuredToolResult'");
  });

  it('keeps extracted surfaces bounded instead of moving the monolith unchanged', () => {
    const limits: Array<[string, number]> = [
      ['./agentDetailPolicy.ts', 420],
      ['./useSessionTransportController.ts', 900],
      ['./SessionLineageSurface.tsx', 700],
      ['./ArtifactSurface.tsx', 500],
      ['./SessionRuntimePanel.tsx', 1200],
      ['./StructuredToolResult.tsx', 650],
    ];

    for (const [path, maximum] of limits) {
      expect(read(path).split('\n').length, path).toBeLessThanOrEqual(maximum);
    }
  });
});
