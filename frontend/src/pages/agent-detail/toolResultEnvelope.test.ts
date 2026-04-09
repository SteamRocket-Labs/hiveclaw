import { describe, expect, it } from 'vitest';

import {
  normalizeToolCallResult,
  parseCreateEmployeeToolResult,
  parsePreviewAgentBlueprintResult,
} from './toolResultEnvelope';

describe('parseCreateEmployeeToolResult', () => {
  it('parses structured JSON payloads', () => {
    const parsed = parseCreateEmployeeToolResult(JSON.stringify({
      status: 'success',
      agent_id: '7a5b31cb-89b4-4053-a48e-6dfb42a8af20',
      agent_name: 'Research Bot',
      message: 'Successfully created digital employee Research Bot.',
    }));

    expect(parsed).toEqual({
      agentId: '7a5b31cb-89b4-4053-a48e-6dfb42a8af20',
      agentName: 'Research Bot',
      message: 'Successfully created digital employee Research Bot.',
      warnings: [],
      manualSteps: [],
      raw: expect.any(String),
    });
  });

  it('keeps backward compatibility with legacy plain-text output', () => {
    const parsed = parseCreateEmployeeToolResult(
      "Successfully created digital employee 'Research Bot' (ID: 7a5b31cb-89b4-4053-a48e-6dfb42a8af20).",
    );

    expect(parsed).toEqual({
      agentId: '7a5b31cb-89b4-4053-a48e-6dfb42a8af20',
      agentName: null,
      message: "Successfully created digital employee 'Research Bot' (ID: 7a5b31cb-89b4-4053-a48e-6dfb42a8af20).",
      warnings: [],
      manualSteps: [],
      raw: expect.any(String),
    });
  });

  it('returns null for unrelated tool output', () => {
    expect(parseCreateEmployeeToolResult('No agent created here.')).toBeNull();
  });

  it('parses preview blueprint payloads into structured preview metadata', () => {
    const parsed = parsePreviewAgentBlueprintResult(JSON.stringify({
      status: 'preview',
      blueprint: {
        name: 'Research Bot',
        deferred_capabilities: ['github-research (defer until the first real task proves it is needed)'],
      },
      summary: {
        mission: 'Research competitors and write briefs.',
        first_mission: 'Create the first competitor landscape brief.',
      },
      ready_now: ['Builtin tools + default skills + memory loop'],
      will_install: ['mcp: github'],
      warnings: ['primary_users is empty — the agent may be less clear about who it serves.'],
      manual_steps: ['Validate the first deliverable before expanding capabilities.'],
    }));

    expect(parsed).toEqual({
      kind: 'hr_preview',
      name: 'Research Bot',
      mission: 'Research competitors and write briefs.',
      firstMission: 'Create the first competitor landscape brief.',
      readyNow: ['Builtin tools + default skills + memory loop'],
      willInstall: ['mcp: github'],
      deferredCapabilities: ['github-research (defer until the first real task proves it is needed)'],
      warnings: ['primary_users is empty — the agent may be less clear about who it serves.'],
      manualSteps: ['Validate the first deliverable before expanding capabilities.'],
    });
  });

  it('normalizes create employee JSON result into user-facing text and agent id', () => {
    const normalized = normalizeToolCallResult(
      'create_digital_employee',
      JSON.stringify({
        status: 'success',
        agent_id: '7a5b31cb-89b4-4053-a48e-6dfb42a8af20',
        message: 'Successfully created digital employee Research Bot.',
        warnings: ['GitHub MCP still needs OAuth.'],
        manual_steps: ['Verify the first mission output before adding more tools.'],
      }),
    );

    expect(normalized.displayResult).toBe('Successfully created digital employee Research Bot.');
    expect(normalized.createdAgentId).toBe('7a5b31cb-89b4-4053-a48e-6dfb42a8af20');
    expect(normalized.raw).toEqual(expect.any(String));
    expect(normalized.toolMeta).toEqual({
      kind: 'create_employee_success',
      agentId: '7a5b31cb-89b4-4053-a48e-6dfb42a8af20',
      agentName: null,
      message: 'Successfully created digital employee Research Bot.',
      warnings: ['GitHub MCP still needs OAuth.'],
      manualSteps: ['Verify the first mission output before adding more tools.'],
    });
  });

  it('normalizes preview blueprint payloads into a structured tool card payload', () => {
    const normalized = normalizeToolCallResult(
      'preview_agent_blueprint',
      JSON.stringify({
        status: 'preview',
        blueprint: {
          name: 'Research Bot',
          deferred_capabilities: ['github-research'],
        },
        summary: {
          mission: 'Research competitors and write briefs.',
          first_mission: 'Create the first competitor landscape brief.',
        },
        ready_now: ['Builtin tools + default skills + memory loop'],
        will_install: [],
        warnings: ['Focus content is empty — the new agent will need an initial mission seed after creation.'],
        manual_steps: ['Confirm the first mission before installing more capabilities.'],
      }),
    );

    expect(normalized.displayResult).toBe('Blueprint preview ready for Research Bot.');
    expect(normalized.createdAgentId).toBeNull();
    expect(normalized.toolMeta).toEqual({
      kind: 'hr_preview',
      name: 'Research Bot',
      mission: 'Research competitors and write briefs.',
      firstMission: 'Create the first competitor landscape brief.',
      readyNow: ['Builtin tools + default skills + memory loop'],
      willInstall: [],
      deferredCapabilities: ['github-research'],
      warnings: ['Focus content is empty — the new agent will need an initial mission seed after creation.'],
      manualSteps: ['Confirm the first mission before installing more capabilities.'],
    });
  });

  it('preserves non-create tool results untouched', () => {
    const normalized = normalizeToolCallResult('web_search', 'raw search result');

    expect(normalized).toEqual({
      displayResult: 'raw search result',
      createdAgentId: null,
      raw: 'raw search result',
      toolMeta: null,
    });
  });

  it('falls back to raw create employee output when payload is not parseable', () => {
    const normalized = normalizeToolCallResult('create_digital_employee', 'temporary downstream error');

    expect(normalized).toEqual({
      displayResult: 'temporary downstream error',
      createdAgentId: null,
      raw: 'temporary downstream error',
      toolMeta: null,
    });
  });
});
