import { describe, expect, it } from 'vitest';

import en from './en.json';
import zh from './zh.json';

describe('local agent i18n keys', () => {
  it('has explicit unknown presence labels in every locale', () => {
    expect(en.localAgents.unknown).toBe('Local agent status unknown');
    expect(zh.localAgents.unknown).toBe('本地 Agent 状态未知');
  });
});
