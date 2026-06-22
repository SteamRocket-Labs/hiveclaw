import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

describe('evolutionApi', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('reads the v2 timeline contract from the agent evolution endpoint', async () => {
    vi.doMock('../core', async () => {
      const actual = await vi.importActual<typeof import('../core')>('../core');
      return {
        ...actual,
        get: vi.fn(),
      };
    });

    const { evolutionApi } = await import('./evolution');
    const { get } = await import('../core');
    vi.mocked(get).mockResolvedValue({
      schema: 'agent_evolution_view.v2',
      path_contract: {},
      lanes: {
        memory: [
          {
            id: 't3-job:job-1',
            lane: 'memory',
            stage: 't3_job',
            status: 'awaiting_agent_review',
            title: 'job-1',
            path: 'memory/.staging/t3_jobs/job-1/manifest.json',
            source_refs: ['t2://session/s1/segment/seg-1'],
          },
        ],
        soul: [],
        skill_ecosystem: [],
        skill_tuning: [],
        legacy_audit: [],
      },
      timeline: [
        {
          id: 't3-job:job-1',
          lane: 'memory',
          stage: 't3_job',
          status: 'awaiting_agent_review',
          title: 'job-1',
          path: 'memory/.staging/t3_jobs/job-1/manifest.json',
          source_refs: ['t2://session/s1/segment/seg-1'],
        },
      ],
      memory_learning: { pending_t3_jobs: [], t3_targets: {} },
      soul: { active_path: 'soul.md', pending_candidates: [] },
      skill_ecosystem: { summary: { total: 0, active: 0, stale: 0, archived: 0, evolvable: 0, by_origin: {} }, skills: [] },
      skill_tuning: { candidates: [] },
      legacy_audit: { detected_legacy_files: [] },
    });

    const payload = await evolutionApi.get('agent-1');

    expect(get).toHaveBeenCalledWith('/agents/agent-1/evolution');
    expect(payload.timeline[0].lane).toBe('memory');
    expect(payload.lanes.memory[0].stage).toBe('t3_job');
    expect(payload.timeline[0].source_refs).toEqual(['t2://session/s1/segment/seg-1']);
  });
});
