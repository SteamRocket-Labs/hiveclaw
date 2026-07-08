import { describe, expect, it } from 'vitest';

async function readSource(relativePath: string): Promise<string> {
  const fsModuleId = 'node:fs';
  const { readFileSync } = (await import(/* @vite-ignore */ fsModuleId)) as {
    readFileSync: (path: URL, encoding: string) => string;
  };
  return readFileSync(new URL(relativePath, import.meta.url), 'utf8');
}

describe('SessionNativeControls Agent Team contract', () => {
  it('keeps context usage diagnostics connected to a real workbench query', async () => {
    const controlsSource = await readSource('./SessionNativeControls.tsx');
    const apiSource = await readSource('../../api/domains/ccParity.ts');

    expect(apiSource).toContain('getSessionContextUsage');
    expect(apiSource).toContain('/context-usage');
    expect(controlsSource).toContain("['session-workbench-context-usage'");
    expect(controlsSource).toContain('ccParityApi.getSessionContextUsage');
    expect(controlsSource).toContain('sessionWorkbench.contextUsage');
    expect(controlsSource).toContain('contextUsageCounts');
    expect(controlsSource).toContain('sessionWorkbench.cacheDecisions');
    expect(controlsSource).toContain('cache_decision_ledger');
  });

  it('keeps Team creation container-only and discovers teammates through spawn_subagent', async () => {
    const controlsSource = await readSource('./SessionNativeControls.tsx');
    const apiSource = await readSource('../../api/domains/ccParity.ts');

    expect(controlsSource).toContain('ccParityApi.createTeam');
    expect(controlsSource).toContain('teamCreateContainerOnly');
    expect(controlsSource).toContain('teammate_creation_tool');
    expect(controlsSource).not.toContain('memberRole');
    expect(controlsSource).not.toContain('teamMemberPlaceholder');
    expect(controlsSource).not.toContain('members: [');

    expect(apiSource).toContain('teammate_creation_tool');
    expect(apiSource).not.toContain('CreateAgentTeamMemberInput');
    expect(apiSource).not.toContain('members?:');
  });
});
