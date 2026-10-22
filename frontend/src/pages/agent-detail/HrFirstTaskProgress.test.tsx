import { renderToStaticMarkup } from 'react-dom/server';
import { beforeEach, expect, it, vi } from 'vitest';
import type { HrCreationDraft } from '../../api/domains/hrCreation';
import { HrBlueprintPreviewCard } from './HrBlueprintPreviewCard';
import { normalizeToolCallResult } from './toolResultEnvelope';

const state = vi.hoisted(() => ({ draft: undefined as HrCreationDraft | undefined, failed: false, viewer: 'owner' }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (_key: string, fallback: string) => fallback }) }));
vi.mock('../../stores', () => ({ useAuthStore: (select: (value: unknown) => unknown) => select({ user: { id: state.viewer } }) }));
vi.mock('@tanstack/react-query', () => ({
  useQuery: () => ({ data: state.draft, isError: state.failed }),
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
  useMutation: () => ({ isPending: false }),
}));

beforeEach(() => {
  state.failed = false;
  state.viewer = 'owner';
  state.draft = {
    status: 'preview', blueprint_id: 'draft-1', blueprint_version: 1, blueprint_hash: 'hash', draft_status: 'completed',
    blueprint: { name: 'Researcher' }, requested_by_user_id: 'owner', hr_agent_id: 'original-agent',
    session_id: 'original-work', created_agent_id: 'employee',
    creation_authorization: { mode: 'policy_direct', first_task: 'Draft the report' },
    first_task_work: { runtime_task_id: 'run-1', agent_id: 'employee', session_id: 'child-1', status: 'running',
      question: null, can_answer: false, permissions: [],
      progress: { model_result_id: 'private-model-reference', event_id: 'private-event-reference',
        text: 'I am preparing the **authorized report**.' },
    },
  } as HrCreationDraft;
});

function originalCard() {
  const preview = normalizeToolCallResult('create_employee_from_request', state.draft).toolMeta;
  if (preview?.kind !== 'hr_preview') throw new Error('Missing original creation preview');
  return renderToStaticMarkup(<HrBlueprintPreviewCard agentId="original-agent" preview={preview} />);
}

it.each(['running', 'resumable', 'suspended'])('renders committed progress in the original %s first task', (status) => {
  state.draft!.first_task_work!.status = status;
  const markup = originalCard();
  expect(markup).toContain('I am preparing the <strong>authorized report</strong>.');
  expect(markup).not.toContain('private-model-reference');
  expect(markup).not.toContain('private-event-reference');
});

it.each(['completed', 'failed', 'killed', 'unavailable', 'unknown'])('hides cached progress for %s work', (status) => {
  state.draft!.first_task_work!.status = status;
  expect(originalCard()).not.toContain('authorized report');
});

it.each(['viewer', 'failed', 'missing'])('hides cached progress on %s evidence', (change) => {
  if (change === 'viewer') state.viewer = 'another-user';
  if (change === 'failed') state.failed = true;
  if (change === 'missing') state.draft!.first_task_work!.progress = null;
  expect(originalCard()).not.toContain('authorized report');
});
