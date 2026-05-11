import { describe, expect, it } from 'vitest';

import { normalizeToolCallResult } from './toolResultEnvelope';

describe('deep research tool result normalization', () => {
  it('recognizes deep_research_start payloads', () => {
    const normalized = normalizeToolCallResult('deep_research_start', JSON.stringify({
      ok: true,
      task_id: 'task-1',
      status: 'running',
      artifact_dir: 'runtime_artifacts/long_tasks/task-1/deep_research',
      next_action: 'Use deep_research_check with this task_id.',
    }));

    expect(normalized.displayResult).toContain('Deep research running');
    expect(normalized.toolMeta).toMatchObject({
      kind: 'deep_research',
      taskId: 'task-1',
      status: 'running',
    });
  });

  it('recognizes completed deep_research_run payloads with quality gates', () => {
    const normalized = normalizeToolCallResult('deep_research_run', JSON.stringify({
      ok: true,
      status: 'completed',
      summary: 'Report complete',
      report_path: 'runtime_artifacts/deep_research/report.md',
      source_count: 4,
      claim_count: 6,
      quality_gates: { attribution: 'passed', completeness: 'passed' },
      gaps: [],
    }));

    expect(normalized.displayResult).toContain('Report complete');
    expect(normalized.toolMeta).toMatchObject({
      kind: 'deep_research',
      status: 'completed',
      sourceCount: 4,
      claimCount: 6,
    });
  });
});
