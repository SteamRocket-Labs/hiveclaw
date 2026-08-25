import { describe, expect, it } from 'vitest';

import enResource from './en.json';
import zhResource from './zh.json';

type ResourceTree = Record<string, unknown>;

function flattenResource(tree: ResourceTree, prefix = ''): Record<string, string> {
  const flat: Record<string, string> = {};
  for (const [key, value] of Object.entries(tree)) {
    const path = prefix ? `${prefix}.${key}` : key;
    if (value && typeof value === 'object') {
      Object.assign(flat, flattenResource(value as ResourceTree, path));
    } else {
      flat[path] = String(value);
    }
  }
  return flat;
}

function declaredVariables(template: string): string[] {
  const names = [...String(template).matchAll(/\{\{\s*(\w+)\s*\}\}/g)].map((match) => match[1]);
  return [...new Set(names)].sort();
}

// Mirrors i18next default interpolation with skipOnVariables=true: a variable
// the caller does not provide stays in the output as the literal {{token}}.
// That literal token leaking into the page is the UI-001 production defect.
function interpolate(template: string, values: Record<string, unknown>): string {
  return String(template).replace(/\{\{\s*(\w+)\s*\}\}/g, (_token, name: string) =>
    name in values ? String(values[name]) : `{{${name}}}`,
  );
}

const enFlat = flattenResource(enResource as ResourceTree);
const zhFlat = flattenResource(zhResource as ResourceTree);

describe('i18n interpolation contract', () => {
  it('renders dashboard.home.summary with the variables the Dashboard call site provides', () => {
    // DashboardHomeShell calls t('dashboard.home.summary', { recent, active }).
    // The persisted en/zh resources must be interpolatable with exactly those
    // variables; an unresolved {{attention}} token is the UI-001 defect.
    const callSiteValues = { recent: 3, active: 2 };
    for (const [locale, flat] of [['en', enFlat], ['zh', zhFlat]] as const) {
      const template = flat['dashboard.home.summary'];
      expect(template, `dashboard.home.summary must exist in ${locale}`).toBeTruthy();
      const rendered = interpolate(template, callSiteValues);
      expect(
        rendered,
        `${locale} dashboard.home.summary must not leak an unresolved token: ${rendered}`,
      ).not.toContain('{{');
    }
  });

  it('declares the same interpolation variables in en and zh for every shared key', () => {
    const mismatches: string[] = [];
    for (const [key, enTemplate] of Object.entries(enFlat)) {
      const zhTemplate = zhFlat[key];
      if (zhTemplate === undefined) continue;
      const enVars = declaredVariables(enTemplate).join(',');
      const zhVars = declaredVariables(zhTemplate).join(',');
      if (enVars !== zhVars) {
        mismatches.push(`${key}: en=[${enVars}] zh=[${zhVars}]`);
      }
    }
    expect(mismatches, `locale interpolation drift:\n${mismatches.join('\n')}`).toEqual([]);
  });

  it('translates the runtime status vocabulary instead of leaking English enum words into zh', () => {
    // agent.runtimeStatus.* is the render vocabulary for every runtime status
    // chip (SessionRuntimePanel); zh must carry real translations.
    const expectedZh: Record<string, string> = {
      ready: '就绪',
      working: '运行中',
      waitingApproval: '等待审批',
      needsAttention: '需要处理',
      needsAdminReview: '需要管理员处理',
    };
    for (const [key, expected] of Object.entries(expectedZh)) {
      expect(zhFlat[`agent.runtimeStatus.${key}`], `agent.runtimeStatus.${key}`).toBe(expected);
    }
    expect(zhFlat['agent.status.idle']).toBe('空闲');
    expect(zhFlat['dashboard.status.idle']).toBe('空闲');
    expect(zhFlat['agent.settings.expiry.setExpiry']).toBe('设置有效期');
  });
});
