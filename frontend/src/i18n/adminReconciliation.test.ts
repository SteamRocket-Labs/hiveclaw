import { describe, expect, it } from 'vitest';

import en from './en.json';
import zh from './zh.json';


const REQUIRED_OPERATION_KEYS = [
  'evidenceIncomplete',
  'evidenceChanged',
  'operationId',
  'operationAction',
  'operationReason',
  'confirmResume',
  'resume',
  'deliveryTenant',
  'deliveryTargetAgent',
  'deliveryTargetUser',
  'deliveryParentSession',
  'deliveryChildSession',
  'deliveryAuthorityValid',
  'deliveryRefreshWarning',
] as const;

describe('admin runtime reconciliation translations', () => {
  it.each([
    ['en', en],
    ['zh', zh],
  ] as const)('defines every operator recovery key in %s', (_locale, messages) => {
    const reconciliation = messages.admin.reconciliation as Record<string, string>;
    for (const key of REQUIRED_OPERATION_KEYS) {
      expect(reconciliation[key]?.trim()).toBeTruthy();
    }
  });
});
