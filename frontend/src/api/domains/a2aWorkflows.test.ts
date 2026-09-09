import { expect, it, vi } from 'vitest';
import { get, post } from '../core';
import { a2aWorkflowApi } from './a2aWorkflows';

vi.mock('../core', () => ({ get: vi.fn(async () => []), post: vi.fn(async () => ({})) }));

it('binds graph start and recovery to the exact owner session, preview and node', async () => {
  const preview = { definition: { name: 'graph' }, definition_hash: 'hash', args: { input: 7 },
    node_order: ['draft'], admissible: true, collaboration_checks: [] };
  await a2aWorkflowApi.start('agent', 'session', 'intent', preview);
  expect(post).toHaveBeenCalledWith('/agents/agent/a2a-workflows/runs', {
    session_id: 'session', run_id: 'intent', definition: preview.definition, definition_hash: 'hash', args: preview.args,
  });
  await a2aWorkflowApi.control('agent', 'intent', 'retry', 'draft');
  expect(post).toHaveBeenLastCalledWith('/agents/agent/a2a-workflows/runs/intent/control', { action: 'retry', node_id: 'draft' });
  await a2aWorkflowApi.list('agent', 'session');
  expect(get).toHaveBeenCalledWith('/agents/agent/a2a-workflows/runs?session_id=session');
});
