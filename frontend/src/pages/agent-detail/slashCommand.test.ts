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

  it('rejects malformed JSON args', () => {
    expect(() => parseSlashCommandInput('/goal_start {bad json}')).toThrow('Invalid slash command JSON');
  });

  it('returns the search query after the slash prefix', () => {
    expect(slashCommandQuery('/goal')).toBe('goal');
    expect(slashCommandQuery('normal text')).toBeNull();
  });
});
