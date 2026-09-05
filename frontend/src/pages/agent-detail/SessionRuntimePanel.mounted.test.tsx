// @vitest-environment jsdom

import React from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallbackOrOptions?: string | Record<string, unknown>, options?: Record<string, unknown>) => {
      if (typeof fallbackOrOptions === 'string') {
        return fallbackOrOptions.replace(/\{\{\s*(\w+)\s*\}\}/g, (_, name) => String(options?.[name] ?? ''));
      }
      const values = (fallbackOrOptions as Record<string, unknown> | undefined) ?? options ?? {};
      if ('count' in values) {
        return `${key.split('.').pop() ?? key}:${String(values.count)}`;
      }
      if ('name' in values) {
        return `${key.split('.').pop() ?? key}:${String(values.name)}`;
      }
      return key.split('.').pop() ?? key;
    },
    i18n: { language: 'en' },
  }),
}));

import { SessionRuntimePanel } from './SessionRuntimePanel';
import type { AgentChatMessage } from './chatRuntime';

afterEach(cleanup);

const ACTIVE_SESSION = { id: 'runtime-panel-session', title: 'Runtime panel session' };

const AGENT = { id: 'agent-1', name: 'Runtime Bot' };

const DELIVERABLE_MESSAGES = [
  {
    role: 'assistant',
    content: 'Generated a report.',
    artifacts: [
      {
        name: 'runtime-report.md',
        path: 'workspace/runtime-report.md',
        previewKind: 'markdown',
        size: 2048,
        runtimeTaskId: 'run-1',
        snapshotHash: 'sha256-runtime',
        sourceAgentName: 'Reviewer Bot',
      },
      {
        name: 'historical-report.md',
        path: 'workspace/historical-report.md',
        previewKind: 'markdown',
        source: 'historical_session',
      },
      {
        name: 'scratch.txt',
        path: 'workspace/scratch.txt',
        previewKind: 'text',
      },
    ],
  },
] as unknown as AgentChatMessage[];

const IDLE_MESSAGES = [
  { role: 'user', content: 'Summarize what is ready.' },
  { role: 'assistant', content: 'Everything is ready.' },
] as unknown as AgentChatMessage[];

function idleWorkbench() {
  return {
    schema: 'session_workbench.v1',
    agent_id: 'agent-1',
    session: { id: 'runtime-panel-session', title: 'Runtime panel session' },
    runtime_sections: {},
    goals: [],
  } as any;
}

function activeWorkbench() {
  return {
    schema: 'session_workbench.v1',
    agent_id: 'agent-1',
    session: { id: 'runtime-panel-session', title: 'Runtime panel session' },
    turn: { truth_source: 't0_events_jsonl', event_count: 14, checkpoint_count: 2 },
    controls: {},
    goals: [{ id: 'goal-1', objective: 'Validate runtime panel', status: 'active' }],
    active_run: { id: 'workflow-run-1', task_type: 'workflow', status: 'running' },
    runtime_tasks: [
      { id: 'workflow-run-1', task_type: 'workflow', title: 'ccplus-closure-audit', status: 'running' },
    ],
    runtime_sections: {
      agent_teams: [
        {
          id: 'team-1',
          runtime_kind: 'agent_team',
          label: 'Research Team',
          status: 'running',
          chat_session_id: 'team-session-1',
          enterable: true,
          members: [
            {
              id: 'member-1',
              runtime_kind: 'team_member',
              label: 'Reviewer',
              elapsed_seconds: 95,
              total_tokens: 3600,
              tool_use_count: 4,
              child_session_id: 'member-session-1',
              enterable: true,
              summary: 'Checking runtime panel evidence.',
              status: 'awaiting_approval',
            },
          ],
        },
      ],
      subagents: [
        {
          id: 'subagent-1',
          runtime_kind: 'subagent',
          label: 'One-shot critic',
          status: 'awaiting_user_clarification',
          child_session_id: 'subagent-session-1',
          enterable: true,
        },
      ],
      peer_a2a: {
        schema: 'hive.ccplus.runtime_section.v1',
        key: 'peer_a2a',
        count: 1,
        items: [
          {
            id: 'a2a-task-1',
            runtime_kind: 'peer_a2a',
            label: 'Finance digital employee',
            status: 'blocked',
            child_session_id: 'a2a-session-1',
            enterable: true,
            summary: 'The target model provider rejected the request.',
          },
        ],
      },
      workflows: [
        {
          id: 'workflow-run-1',
          runtime_kind: 'workflow',
          label: 'ccplus-closure-audit',
          status: 'running',
          elapsed_seconds: 125,
          token_count: 4200,
          tool_count: 3,
          steps: [{ id: 'workflow-step-1', label: 'Review plan', status: 'gate_waiting' }],
          leaf_calls: [{ id: 'workflow-leaf-1', label: 'Leaf check', status: 'completed', enterable: false }],
        },
      ],
      background: [
        {
          id: 'background-run-1',
          runtime_kind: 'background_agent',
          label: 'backend verification',
          status: 'completed',
        },
      ],
      notifications: [
        {
          id: 'wake-1',
          runtime_kind: 'notification',
          label: 'notify user when run completes',
          status: 'pending',
        },
      ],
      runs: [
        {
          id: 'run-1',
          runtime_kind: 'runtime_task',
          label: 'web chat turn',
          status: 'running',
          elapsed_seconds: 25,
          token_count: 900,
          tool_use_count: 1,
        },
      ],
      raw: [
        {
          id: 'raw-1',
          runtime_kind: 'raw_event',
          label: 'runtime_action_completed',
          status: 'completed',
        },
      ],
    },
  } as any;
}

function renderPanel(overrides: Partial<Parameters<typeof SessionRuntimePanel>[0]> = {}) {
  return render(
    <SessionRuntimePanel
      messages={IDLE_MESSAGES}
      sessionWorkbench={idleWorkbench()}
      activeSession={ACTIVE_SESSION}
      agent={AGENT}
      agentId="agent-1"
      sessionId="runtime-panel-session"
      {...overrides}
    />,
  );
}

function textOf(element: HTMLElement): string {
  return element.textContent ?? '';
}

describe('SessionRuntimePanel collapsed rail', () => {
  it('stays a quiet expand affordance when the session has no deliverables or runtime activity', () => {
    const onToggleCollapsed = vi.fn();
    renderPanel({ collapsed: true, onToggleCollapsed });

    const toggle = screen.getByTestId('session-runtime-collapse-toggle');
    expect(toggle.getAttribute('aria-label')).toBe('Expand runtime panel');
    expect(screen.queryByTestId('session-runtime-collapsed-deliverables')).toBeNull();
    expect(screen.queryByTestId('session-runtime-collapsed-attention')).toBeNull();
    expect(screen.queryByTestId('session-runtime-collapsed-running')).toBeNull();
    expect(screen.queryByTestId('session-runtime-console')).toBeNull();

    fireEvent.click(toggle);
    expect(onToggleCollapsed).toHaveBeenCalledTimes(1);
  });

  it('surfaces real deliverable, attention, and running counts as accessible expand actions', () => {
    const onToggleCollapsed = vi.fn();
    renderPanel({
      collapsed: true,
      onToggleCollapsed,
      messages: DELIVERABLE_MESSAGES,
      sessionWorkbench: activeWorkbench(),
      activeRunStatus: 'running',
    });

    const deliverables = screen.getByTestId('session-runtime-collapsed-deliverables');
    expect(textOf(deliverables)).toContain('1');
    expect(deliverables.getAttribute('aria-label')).toBe('Show deliverables (1)');
    // The expand toggle owns the rail's top strip; the first badge directly
    // follows it in DOM order so the two actions keep separate space.
    const toggle = screen.getByTestId('session-runtime-collapse-toggle');
    expect(toggle.nextElementSibling).toBe(deliverables);

    const attention = screen.getByTestId('session-runtime-collapsed-attention');
    expect(textOf(attention)).toContain('4');
    expect(attention.getAttribute('aria-label')).toBe('Show items waiting for you (4)');

    const running = screen.getByTestId('session-runtime-collapsed-running');
    expect(textOf(running)).toContain('3');
    expect(running.getAttribute('aria-label')).toBe('Show running items (3)');

    fireEvent.click(deliverables);
    fireEvent.click(attention);
    fireEvent.click(running);
    expect(onToggleCollapsed).toHaveBeenCalledTimes(3);
  });
});

describe('SessionRuntimePanel expanded empty console', () => {
  it('replaces the zero-count engineering dashboard with one quiet empty state', () => {
    renderPanel({ collapsed: false });

    expect(textOf(screen.getByTestId('session-runtime-console-empty'))).toContain(
      'No background agents, teams, or workflows in this session.',
    );
    expect(screen.queryByTestId('session-runtime-summary-strip')).toBeNull();
    expect(screen.queryByTestId('session-runtime-segment-team')).toBeNull();
    expect(screen.queryByTestId('session-runtime-segment-workers')).toBeNull();
    expect(screen.queryByTestId('session-runtime-segment-workflow')).toBeNull();
    expect(screen.queryByTestId('session-runtime-segment-activity')).toBeNull();
    expect(screen.queryByText('0 total')).toBeNull();
    expect(textOf(screen.getByTestId('session-runtime-deliverables'))).toContain(
      'No delivered artifacts in this session yet.',
    );
  });

  it('keeps goal controls visible when the console is empty', () => {
    const workbench = idleWorkbench();
    workbench.goals = [{ id: 'goal-1', objective: 'Preserve the evidence trail', status: 'active' }];
    renderPanel({ collapsed: false, sessionWorkbench: workbench });

    expect(screen.getByText('Preserve the evidence trail')).toBeTruthy();
    expect(screen.getByTestId('session-runtime-console-empty')).toBeTruthy();
  });
});

describe('SessionRuntimePanel expanded active console', () => {
  it('retains real counts, waiters, segments, and deliverables in the expanded view', () => {
    renderPanel({
      collapsed: false,
      messages: DELIVERABLE_MESSAGES,
      sessionWorkbench: activeWorkbench(),
      activeRunStatus: 'running',
    });

    const deliverables = screen.getByTestId('session-runtime-deliverables');
    expect(textOf(deliverables)).toContain('runtime-report.md');
    expect(textOf(deliverables)).toContain('By Reviewer Bot');
    expect(screen.queryByTestId('session-workspace-documents-historical')).toBeNull();
    expect(textOf(deliverables)).not.toContain('Historical');
    expect(screen.queryByTestId('session-workspace-documents-unattributed')).toBeNull();
    expect(textOf(deliverables)).not.toContain('Unattributed');

    const strip = screen.getByTestId('session-runtime-summary-strip');
    expect(strip.getAttribute('data-runtime-state')).toBe('blocked');
    expect(textOf(strip)).toContain('3 running');
    expect(textOf(strip)).toContain('3 waiting');
    expect(textOf(strip)).toContain('2m 5s');
    expect(textOf(strip)).toContain('8.7K');

    expect(screen.getByTestId('session-runtime-waiters')).toBeTruthy();
    expect(screen.getByTestId('session-runtime-waiter-member-1')).toBeTruthy();
    expect(screen.getByTestId('session-runtime-waiter-subagent-1')).toBeTruthy();
    expect(screen.getByTestId('session-runtime-waiter-workflow-step-1')).toBeTruthy();

    expect(textOf(screen.getByTestId('session-runtime-segment-team'))).toContain('1');
    expect(textOf(screen.getByTestId('session-runtime-segment-a2a'))).toContain('1');
    expect(textOf(screen.getByTestId('session-runtime-segment-workers'))).toContain('1');
    expect(textOf(screen.getByTestId('session-runtime-segment-workflow'))).toContain('1');
    expect(textOf(screen.getByTestId('session-runtime-segment-activity'))).toContain('3');

    const a2aBody = screen.getByTestId('session-runtime-segment-body-a2a');
    expect(textOf(a2aBody)).toContain('Finance digital employee');
    expect(textOf(a2aBody)).toContain('The target model provider rejected the request.');
    expect(textOf(a2aBody)).not.toContain('a2a-task-1');
    expect(textOf(a2aBody)).not.toContain('a2a-session-1');

    const runStatus = screen.getByTestId('session-runtime-run-status');
    expect(textOf(runStatus)).toContain('Reviewer');
    expect(textOf(runStatus)).toContain('One-shot critic');
    expect(textOf(runStatus)).toContain('Review plan');
    expect(textOf(runStatus)).not.toContain('workflow-run-1');
    expect(textOf(runStatus)).not.toContain('ccplus-closure-audit');
  });

  it('derives child-session rows from message evidence without raw identifiers', () => {
    renderPanel({
      collapsed: false,
      messages: [
        {
          role: 'event',
          content: 'Research worker completed.',
          eventType: 'child_session',
          eventTitle: 'Child Session',
          eventStatus: 'completed',
          eventRuntimeTaskId: 'run-1',
          eventChildSessionId: 'child-session-1',
          eventParentSessionId: 'runtime-panel-session',
        },
      ] as unknown as AgentChatMessage[],
    });

    const console_ = screen.getByTestId('session-runtime-console');
    expect(textOf(console_)).toContain('Child Session');
    expect(textOf(console_)).toContain('Research worker completed.');
    expect(textOf(console_)).not.toContain('session:child-session-1');
    expect(textOf(console_)).not.toContain('run:run-1');
  });

  it('keeps raw tool-produced workspace artifacts out of the expanded deliverables list', () => {
    renderPanel({
      collapsed: false,
      messages: [
        {
          role: 'tool_call',
          content: '',
          toolName: 'office_document_apply',
          toolArgs: { path: 'workspace/proposal.docx' },
          toolStatus: 'done',
          toolResult: '{"ok": true}',
          artifacts: [
            {
              id: 'artifact-doc',
              name: 'proposal.docx',
              path: 'workspace/proposal.docx',
              previewKind: 'office',
            },
          ],
        },
      ] as unknown as AgentChatMessage[],
    });

    const deliverables = screen.getByTestId('session-runtime-deliverables');
    expect(textOf(deliverables)).toContain('No delivered artifacts in this session yet.');
    expect(textOf(deliverables)).not.toContain('proposal.docx');
    expect(screen.queryByTestId('session-workspace-documents-current')).toBeNull();
    expect(screen.queryByTestId('session-workspace-documents-unattributed')).toBeNull();
  });

  it('renders one-shot Sub-agent workers with Inspect only, not a follow-up conversation action', () => {
    const workbench = idleWorkbench();
    workbench.runtime_sections = {
      subagents: [
        {
          id: 'subagent-worker-1',
          runtime_kind: 'subagent',
          label: 'One-shot critic',
          status: 'completed',
          child_session_id: 'child-subagent-session',
          enterable: false,
        },
      ],
    };
    renderPanel({ collapsed: false, sessionWorkbench: workbench, onSelectSession: vi.fn() });

    const body = screen.getByTestId('session-runtime-segment-body-workers');
    expect(textOf(body)).toContain('One-shot critic');
    expect(body.querySelector('[data-runtime-action="subagent-worker-inspect"]')).toBeTruthy();
    expect(body.querySelector('[data-runtime-action="subagent-worker-retry"]')).toBeNull();
    expect(textOf(body)).not.toContain('Continue');
  });
});
