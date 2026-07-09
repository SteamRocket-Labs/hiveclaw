import { describe, expect, it } from 'vitest';

import {
  commandResultRecord,
  formatSlashCommandResult,
  getSessionCommandUiAction,
  isSessionControlCommandResult,
} from './sessionCommandResult';
import type { ExecuteCommandResult } from '../../api/domains/ccParity';

describe('sessionCommandResult', () => {
  it('detects typed session control results and extracts ui actions', () => {
    const response: ExecuteCommandResult = {
      ok: true,
      command: 'compact',
      result: {
        ok: true,
        command: 'compact',
        action: 'compacted_context_installed',
        session_id: 'session-1',
        ui_action: {
          type: 'install_compacted_context',
          session_id: 'session-1',
          message: 'Compacted current context.',
        },
        control_event: {
          event_type: 'session_compact',
        },
        debug_payload: {
          replacement_messages: [{ role: 'system', content: 'summary' }],
        },
      },
    };

    expect(isSessionControlCommandResult(response)).toBe(true);
    expect(getSessionCommandUiAction(response)).toEqual({
      type: 'install_compacted_context',
      session_id: 'session-1',
      message: 'Compacted current context.',
    });
    expect(commandResultRecord(response)?.action).toBe('compacted_context_installed');
  });

  it('does not render raw JSON for typed session control results', () => {
    const response: ExecuteCommandResult = {
      ok: true,
      command: 'rewind',
      result: {
        ok: true,
        command: 'rewind',
        action: 'open_checkpoint_selector',
        session_id: 'session-1',
        ui_action: {
          type: 'open_checkpoint_selector',
          checkpoints: [{ checkpoint_event_id: 'event-1', content: 'Start here' }],
        },
        control_event: null,
        debug_payload: {
          checkpoints: [{ checkpoint_event_id: 'event-1', content: 'Start here' }],
        },
      },
    };

    const display = formatSlashCommandResult(response);

    expect(display).toBe('Command rewind completed.');
    expect(display).not.toContain('```json');
    expect(display).not.toContain('checkpoint_event_id');
  });

  it('keeps legacy command messages readable without exposing raw object payloads first', () => {
    const response: ExecuteCommandResult = {
      ok: true,
      command: 'custom',
      result: {
        message: 'Opened workflow catalog.',
        metadata: { raw: true },
      },
    };

    expect(isSessionControlCommandResult(response)).toBe(false);
    expect(formatSlashCommandResult(response)).toBe('Opened workflow catalog.');
  });

  it('formats workspace restore confirmation as a typed session control result', () => {
    const response: ExecuteCommandResult = {
      ok: false,
      command: 'rewind',
      result: {
        ok: false,
        command: 'rewind',
        action: 'workspace_restore_requires_confirmation',
        session_id: 'session-1',
        ui_action: {
          type: 'confirm_workspace_restore',
          level: 'warning',
          message: 'Workspace rewind will restore files from the selected checkpoint.',
          checkpoint_event_id: 'event-1',
          requested_mode: 'workspace',
        },
      },
    };

    expect(isSessionControlCommandResult(response)).toBe(true);
    expect(getSessionCommandUiAction(response)?.type).toBe('confirm_workspace_restore');
    expect(formatSlashCommandResult(response)).toBe('Workspace rewind will restore files from the selected checkpoint.');
  });
});
