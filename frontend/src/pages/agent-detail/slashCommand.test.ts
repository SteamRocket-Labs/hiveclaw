import { describe, expect, it } from 'vitest';

import { parseSlashCommandInput, slashCommandQuery } from './slashCommand';

describe('slashCommand', () => {
  it('ignores ordinary chat input', () => {
    expect(parseSlashCommandInput('please summarize this')).toBeNull();
    expect(parseSlashCommandInput('/')).toBeNull();
  });

  it('parses a slash command with empty args', () => {
    expect(parseSlashCommandInput('/goal_start')).toEqual({
      name: 'goal_start',
      args: {},
    });
  });

  it('parses a slash command with JSON args', () => {
    expect(parseSlashCommandInput('/goal_start {"objective":"finish"}')).toEqual({
      name: 'goal_start',
      args: { objective: 'finish' },
    });
  });

  it('parses task natural args as a current-session todo task', () => {
    expect(parseSlashCommandInput('/task_create Inspect hooks and session resume')).toEqual({
      name: 'task_create',
      args: { subject: 'Inspect hooks and session resume' },
    });
  });

  it('parses delegated task natural args inside the unified task surface', () => {
    expect(parseSlashCommandInput('/task_create delegate Researcher: Collect source evidence')).toEqual({
      name: 'task_create',
      args: { kind: 'delegation', agent_name: 'Researcher', message: 'Collect source evidence' },
    });
  });

  it('parses session side-question and active-turn natural args', () => {
    expect(parseSlashCommandInput('/btw what does this acronym mean?')).toEqual({
      name: 'btw',
      args: { question: 'what does this acronym mean?' },
    });
    expect(parseSlashCommandInput('/turn_steer use the stricter interpretation')).toEqual({
      name: 'turn_steer',
      args: { content: 'use the stricter interpretation' },
    });
    expect(parseSlashCommandInput('/tag cc-parity')).toEqual({
      name: 'tag',
      args: { tags: ['cc-parity'] },
    });
  });

  it('rejects malformed JSON args', () => {
    expect(() => parseSlashCommandInput('/goal_start {bad json}')).toThrow('Invalid slash command JSON');
  });

  it('returns the search query after the slash prefix', () => {
    expect(slashCommandQuery('/goal')).toBe('goal');
    expect(slashCommandQuery('normal text')).toBeNull();
  });
});
