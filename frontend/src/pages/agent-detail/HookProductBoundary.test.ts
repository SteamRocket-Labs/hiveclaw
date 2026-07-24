import { existsSync, readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const read = (relativePath: string) => readFileSync(new URL(relativePath, import.meta.url), 'utf8');

describe('employee runtime product boundary', () => {
  it('keeps runtime hook implementation details out of employee settings and browser adapters', () => {
    const settings = read('./AgentSettingsSection.tsx');
    const ccParityAdapter = read('../../api/domains/ccParity.ts');
    const hookCardUrl = new URL('./HookRuntimeControlCard.tsx', import.meta.url);

    expect(settings).not.toContain('HookRuntimeControlCard');
    expect(settings).not.toContain('agent-hooks');
    expect(ccParityAdapter).not.toContain('HookControlPlane');
    expect(ccParityAdapter).not.toContain('updateHookRuntimeConfig');
    expect(ccParityAdapter).not.toContain('/hooks');
    expect(existsSync(hookCardUrl)).toBe(false);
  });
});
