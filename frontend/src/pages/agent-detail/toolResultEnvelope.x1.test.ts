import { afterEach, expect, it } from 'vitest';
import i18n from '../../i18n';
import { normalizeToolCallResult } from './toolResultEnvelope';

afterEach(async () => { await i18n.changeLanguage('en'); });

it('zh members read platform-written fallback sentences in Chinese', async () => {
  await i18n.changeLanguage('zh');
  const preview = normalizeToolCallResult('create_employee_from_request', JSON.stringify({
    status: 'preview', draft_status: 'awaiting_confirmation',
    blueprint_id: 'draft-x1', blueprint_hash: 'bp_x1',
    blueprint: { name: '周报助手', primary_users: ['验收成员'], core_outputs: ['周报'], boundaries: '只做整理' },
    warnings: [], manual_steps: [],
  }));
  expect(preview.displayResult).toBe('「周报助手」的创建方案已就绪。');
  expect(preview.displayResult).not.toMatch(/Blueprint|preview ready/);

  const dispatched = normalizeToolCallResult('delegate_to_agent', JSON.stringify({
    ok: true, status: 'queued', first_task_draft_id: 'draft-x1', runtime_task_id: 'task-x1',
  }));
  expect(dispatched.displayResult).toBe('任务已派发，进度和成果会返回当前工作。');
});
