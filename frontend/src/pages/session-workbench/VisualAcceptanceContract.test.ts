import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const read = (relativePath: string) => readFileSync(new URL(relativePath, import.meta.url), 'utf8');

describe('visual acceptance contract', () => {
  it('keeps ordinary/operator and critical runtime states under fresh screenshot and accessibility gates', () => {
    const spec = read('../../../e2e/thread-workbench.spec.ts');

    expect(spec).toContain("{ audience: 'user', scenario: 'active' }");
    expect(spec).toContain("{ audience: 'user', scenario: 'idle' }");
    expect(spec).toContain("{ audience: 'operator', scenario: 'active' }");
    expect(spec).toContain("theme: 'dark'");
    expect(spec).toContain("itemType: 'approval_request'");
    expect(spec).toContain("itemType: 'error'");
    expect(spec).toContain("itemType: 'subagent_activity'");
    expect(spec).toContain("itemType: 'workflow_activity'");
    expect(spec).toContain('release-report.md');
    expect(spec).toContain('Evidence comparison branch');
    expect(spec).toContain('toHaveScreenshot');
    expect(spec).toContain('AxeBuilder');
  });

  it('runs frontend unit, build, audit, browser, screenshot, and accessibility gates in CI', () => {
    const workflow = read('../../../../.github/workflows/harness-ci.yml');

    expect(workflow).toContain('frontend-visual:');
    expect(workflow).toContain('npm ci');
    expect(workflow).toContain('npm audit --omit=dev');
    expect(workflow).toContain('npm test -- --run');
    expect(workflow).toContain('npm run build');
    expect(workflow).toContain('npx playwright install --with-deps chromium');
    expect(workflow).toContain('npm run test:e2e');
  });
});
