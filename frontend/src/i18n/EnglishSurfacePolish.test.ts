/**
 * B2 r3: English polish (r2-en-02). Counts use the i18next plural forms the
 * project already uses (knowledge.docUnit_one/_other); step titles and the
 * approval badge read in sentence case, matching toolTitle.* ("Read file").
 */
import { describe, expect, it } from 'vitest';

import en from './en.json';
import { approvalActionFor } from '../pages/session-workbench/approvalActionPresentation';

const leaf = (path: string): string => path.split('.').reduce<unknown>((node, part) => {
  if (node && typeof node === 'object' && part in (node as Record<string, unknown>)) {
    return (node as Record<string, unknown>)[part];
  }
  return undefined;
}, en) as string;

describe('English surface polish (B2 r3)', () => {
  it('uses the i18next plural count forms for counts of one', () => {
    expect(leaf('agent.chat.disclosure.aggregate.files_one')).toBe('Read {{count}} file');
    expect(leaf('agent.chat.disclosure.aggregate.searches_one')).toBe('Searched the web {{count}} time');
    expect(leaf('agent.chat.disclosure.aggregate.commands_one')).toBe('Ran {{count}} command');
    expect(leaf('agent.chat.disclosure.stepCount_one')).toBe('{{count}} step');
    // The chip that showed "1 files" (r2-en-02).
    expect(leaf('agent.chat.artifacts.count_one')).toBe('{{count}} file');
    // The other form keeps the plural.
    expect(leaf('agent.chat.disclosure.aggregate.files_other')).toBe('Read {{count}} files');
    expect(leaf('agent.chat.disclosure.stepCount_other')).toBe('{{count}} steps');
  });

  it('renders step titles and the approval badge in sentence case', () => {
    expect(leaf('sessionWorkbench.threadItem.status.waiting_user')).toBe('Waiting for approval');
    expect(leaf('sessionWorkbench.approvalAction.createEmployee')).toBe('Create a digital employee');
    expect(leaf('sessionWorkbench.approvalAction.sendEmail')).toBe('Send an email');
    expect(leaf('sessionWorkbench.approvalAction.generic')).toBe('An externally visible action');
    expect(leaf('sessionWorkbench.approvalAction.feishuDoc')).toBe('Use Feishu documents');
    // The code-side English fallbacks stay in sync with the catalog.
    expect(approvalActionFor('create_employee_from_request')?.fallback).toBe('Create a digital employee');
    expect(approvalActionFor('send_email')?.fallback).toBe('Send an email');
    expect(approvalActionFor('feishu_doc_rename')?.fallback).toBe('Use Feishu documents');
  });
});
