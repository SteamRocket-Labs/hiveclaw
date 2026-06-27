import { describe, expect, it } from 'vitest';

import { parseSlashCommandInput, slashCommandQuery } from './slashCommand';

describe('slashCommand', () => {
  it('ignores ordinary chat input', () => {
    expect(parseSlashCommandInput('please summarize this')).toBeNull();
    expect(parseSlashCommandInput('/')).toBeNull();
  });

  it('parses a user-facing alias command with empty args', () => {
    expect(parseSlashCommandInput('/goal')).toEqual({
      name: 'goal',
      args: {},
    });
  });

  it('parses a user-facing alias command with JSON args', () => {
    expect(parseSlashCommandInput('/goal {"objective":"finish"}')).toEqual({
      name: 'goal',
      args: { objective: 'finish' },
    });
  });

  it('parses task natural args as a current-session todo task', () => {
    expect(parseSlashCommandInput('/task Inspect hooks and session resume')).toEqual({
      name: 'task',
      args: { subject: 'Inspect hooks and session resume' },
    });
  });

  it('parses delegated task natural args inside the unified task surface', () => {
    expect(parseSlashCommandInput('/task delegate Researcher: Collect source evidence')).toEqual({
      name: 'task',
      args: { kind: 'delegation', agent_name: 'Researcher', message: 'Collect source evidence' },
    });
  });

  it('parses direct agent delegation as a user wrapper command', () => {
    expect(parseSlashCommandInput('/agent Researcher: Collect source evidence')).toEqual({
      name: 'agent',
      args: { agent_name: 'Researcher', message: 'Collect source evidence', input: 'Researcher: Collect source evidence' },
    });
  });

  it('parses schedule, once, workflow, and skill wrapper commands as natural prompt commands', () => {
    expect(parseSlashCommandInput('/schedule 每天早上九点检查日志')).toEqual({
      name: 'schedule',
      args: { input: '每天早上九点检查日志', instruction: '每天早上九点检查日志' },
    });
    expect(parseSlashCommandInput('/once 明天上午生成报告')).toEqual({
      name: 'once',
      args: { input: '明天上午生成报告', instruction: '明天上午生成报告' },
    });
    expect(parseSlashCommandInput('/workflow triage inbound leads')).toEqual({
      name: 'workflow',
      args: { input: 'triage inbound leads', description: 'triage inbound leads' },
    });
    expect(parseSlashCommandInput('/skill market-research summarize competitors')).toEqual({
      name: 'skill',
      args: { input: 'market-research summarize competitors' },
    });
  });

  it('supports CC-style colon command names for namespaced skills', () => {
    expect(parseSlashCommandInput('/product-design:review audit this flow')).toEqual({
      name: 'product-design:review',
      args: { input: 'audit this flow' },
    });
  });

  it('does not parse hidden or internal commands as user slash commands', () => {
    for (const input of [
      '/btw what does this acronym mean?',
      '/turn_steer use the stricter interpretation',
      '/tag cc-parity',
      '/goal_start finish this',
      '/task_output runtime-1',
      '/copy',
      '/export',
      '/rollback',
      '/checkpoints',
      '/interrupt',
    ]) {
      expect(parseSlashCommandInput(input)).toBeNull();
    }
  });

  it('rejects malformed JSON args', () => {
    expect(() => parseSlashCommandInput('/goal {bad json}')).toThrow('Invalid slash command JSON');
  });

  it('returns the search query after the slash prefix', () => {
    expect(slashCommandQuery('/goal')).toBe('goal');
    expect(slashCommandQuery('normal text')).toBeNull();
  });
});
