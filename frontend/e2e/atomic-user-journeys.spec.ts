import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, test, type APIRequestContext, type APIResponse, type Page, type PlaywrightWorkerArgs } from '@playwright/test';


type Journey = {
  id: string;
  name: string;
  browser_assertions: string[];
  faults: string[];
};

type AuthState = {
  access_token: string;
  user: Record<string, unknown>;
};

type SessionRun = {
  session: { id: string; [key: string]: unknown };
  run: { run_id: string; status: string; [key: string]: unknown };
};

type JourneyContext = {
  ownerApi: APIRequestContext;
  memberApi: APIRequestContext;
  intruderApi: APIRequestContext;
  fakeApi: APIRequestContext;
  agentId: string;
  owner: AuthState;
  member: AuthState;
};

type JourneyEvidence = {
  sessionId: string;
  runId: string;
  transcript: Array<Record<string, unknown>>;
  domain: Record<string, unknown>;
  browserAgentId?: string;
  browserSessionId?: string;
  browserToken?: string;
  browserUser?: Record<string, unknown>;
  expectedText?: string;
};

const HIVE_JOURNEY_BACKEND_URL = process.env.HIVE_JOURNEY_BACKEND_URL || 'http://127.0.0.1:8008';
const HIVE_JOURNEY_FAKE_URL = process.env.HIVE_JOURNEY_FAKE_URL || 'http://127.0.0.1:8010';
const currentDir = path.dirname(fileURLToPath(import.meta.url));
const manifestPath = path.resolve(currentDir, '../../acceptance/atomic_user_journeys.v1.json');
const JOURNEYS = (JSON.parse(fs.readFileSync(manifestPath, 'utf8')) as { journeys: Journey[] }).journeys;

let ownerApi: APIRequestContext;
let memberApi: APIRequestContext;
let intruderApi: APIRequestContext;
let fakeApi: APIRequestContext;
let owner: AuthState;
let member: AuthState;
let agentId = '';


async function responseJson<T>(response: APIResponse, label: string): Promise<T> {
  if (!response.ok()) {
    throw new Error(`${label} failed (${response.status()}): ${(await response.text()).slice(0, 1200)}`);
  }
  return response.json() as Promise<T>;
}


async function authContext(playwright: PlaywrightWorkerArgs['playwright'], auth: AuthState): Promise<APIRequestContext> {
  return playwright.request.newContext({
    baseURL: HIVE_JOURNEY_BACKEND_URL,
    timeout: 120_000,
    extraHTTPHeaders: {
      Authorization: `Bearer ${auth.access_token}`,
      ...(auth.user.tenant_id ? { 'X-Tenant-ID': String(auth.user.tenant_id) } : {}),
    },
  });
}


async function registerOrLogin(
  publicApi: APIRequestContext,
  username: string,
  email: string,
): Promise<AuthState> {
  const password = 'AtomicPass123!';
  const registration = await publicApi.post('/api/auth/register', {
    data: { username, email, password, display_name: username },
  });
  if (registration.ok()) return registration.json() as Promise<AuthState>;
  expect(registration.status(), await registration.text()).toBe(409);
  return responseJson<AuthState>(
    await publicApi.post('/api/auth/login', { data: { username, password } }),
    `login ${username}`,
  );
}


async function login(publicApi: APIRequestContext, username: string): Promise<AuthState> {
  return responseJson<AuthState>(
    await publicApi.post('/api/auth/login', {
      data: { username, password: 'AtomicPass123!' },
    }),
    `refresh login ${username}`,
  );
}


async function bootstrap(playwright: PlaywrightWorkerArgs['playwright']): Promise<void> {
  const publicApi = await playwright.request.newContext({ baseURL: HIVE_JOURNEY_BACKEND_URL, timeout: 120_000 });
  owner = await registerOrLogin(publicApi, 'atomic_owner', 'atomic.owner@example.com');
  if (!owner.user.tenant_id) {
    const ownerPreTenant = await authContext(playwright, owner);
    await responseJson(
      await ownerPreTenant.post('/api/tenants/self-create', { data: { name: 'Atomic Journey Tenant' } }),
      'create journey tenant',
    );
    await ownerPreTenant.dispose();
    owner = await login(publicApi, 'atomic_owner');
  }
  ownerApi = await authContext(playwright, owner);

  const models = await responseJson<Array<Record<string, unknown>>>(
    await ownerApi.get('/api/enterprise/llm-models'),
    'list controlled models',
  );
  let model = models.find((item) => item.label === 'Atomic Controlled Provider');
  if (!model) {
    model = await responseJson<Record<string, unknown>>(
      await ownerApi.post('/api/enterprise/llm-models', {
        data: {
          provider: 'openai',
          model: 'gpt-4o-mini',
          api_key: 'atomic-controlled-key',
          base_url: `${HIVE_JOURNEY_FAKE_URL}/v1`,
          label: 'Atomic Controlled Provider',
          enabled: true,
          max_output_tokens: 2048,
          max_input_tokens: 128000,
        },
      }),
      'create controlled model',
    );
  } else if (
    model.base_url !== `${HIVE_JOURNEY_FAKE_URL}/v1` ||
    model.max_input_tokens !== 128000 ||
    model.max_output_tokens !== 2048
  ) {
    model = await responseJson<Record<string, unknown>>(
      await ownerApi.put(`/api/enterprise/llm-models/${model.id}`, {
        data: {
          api_key: 'atomic-controlled-key',
          base_url: `${HIVE_JOURNEY_FAKE_URL}/v1`,
          enabled: true,
          max_input_tokens: 128000,
          max_output_tokens: 2048,
        },
      }),
      'rebind controlled model to this isolated fake and repair stale contract',
    );
  }

  const agents = await responseJson<Array<Record<string, unknown>>>(await ownerApi.get('/api/agents/'), 'list agents');
  let agent = agents.find((item) => item.name === 'Atomic Journey Agent');
  if (!agent) {
    agent = await responseJson<Record<string, unknown>>(
      await ownerApi.post('/api/agents/', {
        data: {
          name: 'Atomic Journey Agent',
          role_description: 'Exercises all production user-journey contracts against controlled external providers.',
          primary_model_id: model.id,
          permission_scope_type: 'company',
          permission_access_level: 'manage',
        },
      }),
      'create journey agent',
    );
  }
  agentId = String(agent.id);

  member = await registerOrLogin(publicApi, 'atomic_member', 'atomic.member@example.com');
  if (!member.user.tenant_id) {
    const invitation = await responseJson<{ codes: string[] }>(
      await ownerApi.post('/api/enterprise/invitation-codes', { data: { count: 1, max_uses: 1 } }),
      'create member invitation',
    );
    const memberPreJoin = await authContext(playwright, member);
    const joined = await responseJson<{ access_token: string }>(
      await memberPreJoin.post('/api/tenants/join', { data: { invitation_code: invitation.codes[0] } }),
      'join member to owner tenant',
    );
    await memberPreJoin.dispose();
    member = { ...(await login(publicApi, 'atomic_member')), access_token: joined.access_token };
  }
  member = await login(publicApi, 'atomic_member');
  memberApi = await authContext(playwright, member);

  let intruder = await registerOrLogin(publicApi, 'atomic_intruder', 'atomic.intruder@example.com');
  if (!intruder.user.tenant_id) {
    const intruderPreTenant = await authContext(playwright, intruder);
    await responseJson(
      await intruderPreTenant.post('/api/tenants/self-create', { data: { name: 'Atomic Intruder Tenant' } }),
      'create isolated intruder tenant',
    );
    await intruderPreTenant.dispose();
    intruder = await login(publicApi, 'atomic_intruder');
  }
  intruderApi = await authContext(playwright, intruder);
  fakeApi = await playwright.request.newContext({ baseURL: HIVE_JOURNEY_FAKE_URL, timeout: 30_000 });
  await publicApi.dispose();
}


const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function canonicalWaitingPermissionItemId(item: Record<string, unknown>): string | null {
  // Authoritative waiting permission shape: item_kind tool_permission with
  // lifecycle waiting; its top-level item_id is the SessionToolInvocation
  // permission_item_id the resolve route matches. Exact typed checks only —
  // never legacy JSON scanning.
  if (item.item_kind !== 'tool_permission' || item.lifecycle !== 'waiting') return null;
  const itemId = String(item.item_id || '');
  return UUID_PATTERN.test(itemId) ? itemId : null;
}

function canonicalRunId(item: Record<string, unknown>): string {
  const scope = item.scope as Record<string, unknown> | undefined;
  return String(item.run_id || scope?.run_id || '');
}

function normalizeRunId(value: string): string {
  // Run receipts expose the RuntimeTask id as dashless hex; canonical
  // envelopes carry the dashed UUID. Normalize both before binding.
  return value.replace(/-/g, '').toLowerCase();
}

function hasCanonicalTerminalProof(
  canonical: Array<Record<string, unknown>>,
  journeyId: string,
  runId: string,
): boolean {
  // Mechanical terminal proof from the canonical Session V2 truth, bound to
  // the exact run: an assistant_text snapshot whose payload carries the exact
  // journey receipt AND the run_outcome.terminal_committed event, both with a
  // run id equal to the awaited run. Never legacy assistant_message/chunk
  // compatibility rows, and never a receipt from one run paired with another
  // run's terminal outcome.
  const receipt = `${journeyId} terminal receipt from the controlled provider.`;
  const awaitedRunId = normalizeRunId(runId);
  const boundToRun = (item: Record<string, unknown>) => normalizeRunId(canonicalRunId(item)) === awaitedRunId;
  const hasReceiptSnapshot = canonical.some(
    (item) => item.item_kind === 'assistant_text'
      && item.lifecycle === 'snapshot'
      && boundToRun(item)
      && String((item.payload as Record<string, unknown> | undefined)?.content || '').includes(receipt),
  );
  const hasTerminalOutcome = canonical.some(
    (item) => item.item_kind === 'run_outcome' && item.lifecycle === 'terminal_committed' && boundToRun(item),
  );
  return hasReceiptSnapshot && hasTerminalOutcome;
}


async function startAndAwaitChat(
  api: APIRequestContext,
  currentAgentId: string,
  journeyId: string,
  options: { receiptOnly?: boolean; title?: string } = {},
): Promise<{ run: SessionRun; transcript: Array<Record<string, unknown>> }> {
  const content = `${journeyId} exercise the production journey contract${options.receiptOnly ? ' receipt-only' : ''}.`;
  const run = await responseJson<SessionRun>(
    await api.post(`/api/agents/${currentAgentId}/sessions/runs`, {
      data: { content, display_content: content, title: options.title || `${journeyId} atomic journey` },
    }),
    `${journeyId} start session run`,
  );
  const resolved = new Set<string>();
  let latest: Array<Record<string, unknown>> = [];
  await expect.poll(async () => {
    // Poll the canonical schema_version=2 truth for the mechanical terminal
    // proof; legacy compatibility rows are not depended on and may not exist.
    latest = await responseJson<Array<Record<string, unknown>>>(
      await api.get(`/api/agents/${currentAgentId}/sessions/${run.session.id}/transcript?schema_version=2`),
      `${journeyId} canonical transcript`,
    );
    for (const item of latest) {
      const permissionItemId = canonicalWaitingPermissionItemId(item);
      if (!permissionItemId || resolved.has(permissionItemId)) continue;
      const resolution = await api.post(
        `/api/agents/${currentAgentId}/sessions/${run.session.id}/permissions/${permissionItemId}/resolve`,
        { data: { action: 'allow_once', feedback: 'Atomic journey controlled approval' } },
      );
      if (resolution.ok() || resolution.status() === 409) resolved.add(permissionItemId);
    }
    return hasCanonicalTerminalProof(latest, journeyId, String(run.run.run_id));
  }, { timeout: 90_000, intervals: [250, 500, 1000] }).toBe(true);
  // A streamed assistant chunk is user-visible before the durable RuntimeTask
  // has necessarily finished post-run hooks, evidence projection, and lease
  // release. Do not start the next domain action against that half-terminal
  // state: it creates false overlap (for example an active Goal beside the
  // still-running base turn) and is exactly the recovery gap this suite guards.
  await expect.poll(async () => {
    const activeResponse = await api.get(
      `/api/agents/${currentAgentId}/sessions/${run.session.id}/runs/active`,
    );
    if (activeResponse.status() === 404) return true;
    const active = await responseJson<Record<string, unknown> | null>(
      activeResponse,
      `${journeyId} active run drain`,
    );
    return active === null || !['pending', 'running'].includes(String(active.status || ''));
  }, { timeout: 90_000, intervals: [250, 500, 1000] }).toBe(true);
  latest = await responseJson<Array<Record<string, unknown>>>(
    await api.get(`/api/agents/${currentAgentId}/sessions/${run.session.id}/transcript`),
    `${journeyId} terminal ThreadItem transcript replay`,
  );
  return { run, transcript: latest };
}


async function stableTranscriptEvidence(
  api: APIRequestContext,
  currentAgentId: string,
  sessionId: string,
): Promise<Array<Record<string, unknown>>> {
  const first = await responseJson<Array<Record<string, unknown>>>(
    await api.get(`/api/agents/${currentAgentId}/sessions/${sessionId}/transcript`),
    'first transcript replay',
  );
  const second = await responseJson<Array<Record<string, unknown>>>(
    await api.get(`/api/agents/${currentAgentId}/sessions/${sessionId}/transcript`),
    'second transcript replay',
  );
  const sequences = second.map((item) => Number(item.sequence));
  expect(sequences).toEqual([...sequences].sort((a, b) => a - b));
  expect(new Set(sequences).size).toBe(sequences.length);
  expect(second.slice(0, first.length).map((item) => item.id)).toEqual(first.map((item) => item.id));
  return second;
}


function expectCanonicalSuccessfulToolClosure(
  canonical: Array<Record<string, unknown>>,
  runId: string,
): void {
  // Common mechanical closure: collect the union of non-empty invocation ids
  // across ALL run-bound tool_call and tool_result rows — orphan terminal
  // calls, orphan results, duplicate started rows, and blank-invocation rows
  // all fail. For every union member: exactly one started tool_call, exactly
  // one terminal tool_call that is lifecycle completed with payload outcome
  // success, and exactly one tool_result of the same invocation with outcome
  // success; progress rows may be zero or more. Zero tool activity is valid
  // only when there is no bound tool activity at all. No natural language is
  // inspected.
  const payloadOf = (item: Record<string, unknown>) => (item.payload as Record<string, unknown> | undefined) || {};
  const awaitedRunId = normalizeRunId(runId);
  const boundToRun = (item: Record<string, unknown>) => normalizeRunId(canonicalRunId(item)) === awaitedRunId;
  const invocationOf = (item: Record<string, unknown>) => String(item.invocation_id || '');
  const callRows = canonical.filter((item) => item.item_kind === 'tool_call' && boundToRun(item));
  const resultRows = canonical.filter((item) => item.item_kind === 'tool_result' && boundToRun(item));
  if (callRows.length === 0 && resultRows.length === 0) return;
  for (const row of [...callRows, ...resultRows]) {
    expect(invocationOf(row)).not.toBe('');
  }
  const invocationIds = new Set([...callRows, ...resultRows].map(invocationOf).filter(Boolean));
  for (const invocationId of invocationIds) {
    const invocationCalls = callRows.filter((item) => invocationOf(item) === invocationId);
    expect(invocationCalls.filter((item) => item.lifecycle === 'started')).toHaveLength(1);
    const terminalRows = invocationCalls.filter((item) =>
      ['completed', 'failed', 'denied', 'unavailable', 'cancelled', 'reconciled', 'needs_reconciliation'].includes(String(item.lifecycle)),
    );
    expect(terminalRows).toHaveLength(1);
    expect(String(terminalRows[0].lifecycle)).toBe('completed');
    expect(String(payloadOf(terminalRows[0]).outcome || '')).toBe('success');
    const results = resultRows.filter((item) => invocationOf(item) === invocationId);
    expect(results).toHaveLength(1);
    expect(String(payloadOf(results[0]).outcome || '')).toBe('success');
  }
}


async function expectJourneyEvidence(
  journey: Journey,
  context: JourneyContext,
  evidence: JourneyEvidence,
): Promise<void> {
  const replay = await stableTranscriptEvidence(
    context.ownerApi,
    context.agentId,
    evidence.sessionId,
  );
  // Terminal receipt proof is asserted against a fresh canonical
  // schema_version=2 fetch, mechanically (snapshot receipt + terminal
  // outcome); the default ThreadItem replay below stays as the stable
  // read-back check.
  const canonicalReplay = await responseJson<Array<Record<string, unknown>>>(
    await context.ownerApi.get(
      `/api/agents/${context.agentId}/sessions/${evidence.sessionId}/transcript?schema_version=2`,
    ),
    `${journey.id} canonical terminal proof replay`,
  );
  expect(hasCanonicalTerminalProof(canonicalReplay, journey.id, evidence.runId)).toBe(true);
  expectCanonicalSuccessfulToolClosure(canonicalReplay, evidence.runId);
  expect(replay.length).toBeGreaterThanOrEqual(evidence.transcript.length);

  const denied = await context.intruderApi.get(
    `/api/agents/${context.agentId}/sessions/${evidence.sessionId}/transcript`,
  );
  expect([403, 404]).toContain(denied.status());

  const workbench = await responseJson<Record<string, unknown>>(
    await context.ownerApi.get(`/api/agents/${context.agentId}/sessions/${evidence.sessionId}/workbench`),
    `${journey.id} workbench evidence`,
  );
  expect(workbench.schema).toBe('hive.ccplus.session_workbench.v1');
  await test.info().attach(`${journey.id}-mechanical-evidence.json`, {
    body: Buffer.from(JSON.stringify({ journey, transcript: replay, workbench, domain: evidence.domain }, null, 2)),
    contentType: 'application/json',
  });
}


async function setPageAuth(page: Page, auth: AuthState): Promise<void> {
  await page.addInitScript(({ token, tenantId }) => {
    localStorage.setItem('token', token);
    localStorage.setItem('i18nextLng', 'en');
    if (tenantId) localStorage.setItem('current_tenant_id', tenantId);
  }, { token: auth.access_token, tenantId: String(auth.user.tenant_id || '') });
}


async function exerciseDomain(
  journey: Journey,
  base: { run: SessionRun; transcript: Array<Record<string, unknown>> },
  context: JourneyContext,
): Promise<JourneyEvidence> {
  const sessionId = base.run.session.id;
  const domain: Record<string, unknown> = {};
  const suffix = sessionId.slice(0, 8);

  switch (journey.id) {
    case 'J-01': {
      domain.activeRun = await responseJson(
        await context.ownerApi.get(`/api/agents/${context.agentId}/sessions/${sessionId}/runs/active`),
        'read terminal active run',
      ).catch(() => null);
      break;
    }
    case 'J-02': {
      const filename = `j02-${suffix}-deliverable.md`;
      const uploaded = await responseJson<Record<string, unknown>>(
        await context.ownerApi.post('/api/chat/upload', {
          multipart: {
            agent_id: context.agentId,
            skip_personal_kb: 'true',
            file: { name: filename, mimeType: 'text/markdown', buffer: Buffer.from('# J-02 deliverable\n') },
          },
        }),
        'upload deliverable',
      );
      const files = await responseJson<Array<Record<string, unknown>>>(
        await context.ownerApi.get(`/api/agents/${context.agentId}/files/?path=workspace/uploads`),
        'list workspace deliverables',
      );
      expect(JSON.stringify(files)).toContain(filename);
      domain.uploaded = uploaded;
      domain.files = files;
      break;
    }
    case 'J-03': {
      let plan = await responseJson<Record<string, unknown>>(
        await context.ownerApi.post(`/api/agents/${context.agentId}/plans`, {
          data: {
            original_request: 'J-03 prepare an immutable governed plan and wait for confirmation.',
            intent_type: 'in_session_execution',
            source: 'atomic_user_journey',
            session_id: sessionId,
          },
          timeout: 120_000,
        }),
        'author canonical plan',
      );
      await expect.poll(async () => {
        plan = await responseJson<Record<string, unknown>>(
          await context.ownerApi.get(`/api/agents/${context.agentId}/plans/${plan.id}`),
          'poll authored canonical plan',
        );
        return String(plan.status);
      }, { timeout: 90_000, intervals: [250, 500, 1000] }).toMatch(/awaiting_confirmation|planning_failed/);
      expect(plan.status).toBe('awaiting_confirmation');
      const confirmed = await responseJson<Record<string, unknown>>(
        await context.ownerApi.post(`/api/agents/${context.agentId}/plans/${plan.id}/confirm`, {
          data: { plan_version: plan.plan_version, plan_hash: plan.plan_hash, reason: 'Atomic acceptance' },
        }),
        'confirm canonical plan',
      );
      expect(confirmed.status).toBe('confirmed');
      domain.plan = plan;
      domain.confirmed = confirmed;
      break;
    }
    case 'J-04': {
      const requestId = crypto.randomUUID();
      const body = {
        request_id: requestId,
        objective: `J-04 durable goal ${suffix}`,
        token_budget: 4000,
        max_continuation_turns: 2,
        time_budget_seconds: 120,
        start_immediately: false,
      };
      const first = await responseJson<Record<string, unknown>>(
        await context.ownerApi.post(`/api/agents/${context.agentId}/sessions/${sessionId}/goals`, { data: body }),
        'start durable goal',
      );
      const replay = await responseJson<Record<string, unknown>>(
        await context.ownerApi.post(`/api/agents/${context.agentId}/sessions/${sessionId}/goals`, { data: body }),
        'replay durable goal request',
      );
      expect(replay.id).toBe(first.id);
      domain.goal = first;
      domain.replay = replay;
      break;
    }
    case 'J-05': {
      const schedule = await responseJson<Record<string, unknown>>(
        await context.ownerApi.post(`/api/agents/${context.agentId}/schedules/`, {
          data: {
            name: `J-05 controlled schedule ${suffix}`,
            instruction: 'Record a controlled delivery receipt.',
            cron_expr: '0 9 * * *',
            is_enabled: false,
          },
        }),
        'create disabled governed schedule',
      );
      const schedules = await responseJson<Array<Record<string, unknown>>>(
        await context.ownerApi.get(`/api/agents/${context.agentId}/schedules/`),
        'list schedules',
      );
      expect(schedules.some((item) => item.id === schedule.id)).toBe(true);
      domain.schedule = schedule;
      break;
    }
    case 'J-06': {
      const canonical = await responseJson<Array<Record<string, unknown>>>(
        await context.ownerApi.get(
          `/api/agents/${context.agentId}/sessions/${sessionId}/transcript?schema_version=2`,
        ),
        'read canonical V2 transcript',
      );
      const anchor = canonical.find(
        (item) =>
          item.item_kind === 'human_input'
          && item.lifecycle === 'accepted'
          && String((item.actor as Record<string, unknown> | undefined)?.type || '') === 'user',
      );
      expect(anchor?.event_id).toBeTruthy();
      const anchorEventId = String(anchor?.event_id);
      const anchorPayload = (anchor?.payload as Record<string, unknown> | undefined) || {};
      expect(JSON.stringify(anchorPayload.content_parts)).toContain('J-06 exercise the production journey contract');
      const branch = await responseJson<Record<string, unknown>>(
        await context.ownerApi.post(`/api/agents/${context.agentId}/sessions/${sessionId}/branches`, {
          data: {
            mode: 'branch',
            anchor_event_id: anchorEventId,
            title: `J-06 branch ${suffix}`,
            start_run: false,
          },
        }),
        'create session branch',
      );
      const branchInfo = (branch.branch as Record<string, unknown>) || {};
      expect(String(branchInfo.anchor_event_id)).toBe(anchorEventId);
      expect(String(branchInfo.draft_content)).toBe('J-06 exercise the production journey contract.');
      const lineage = await responseJson<Array<Record<string, unknown>>>(
        await context.ownerApi.get(`/api/agents/${context.agentId}/sessions/${String((branch.session as Record<string, unknown>).id)}/lineage`),
        'read branch lineage',
      );
      expect(lineage.length).toBeGreaterThanOrEqual(2);
      domain.branch = branch;
      domain.lineage = lineage;
      domain.anchor = anchor;
      break;
    }
    case 'J-07': {
      const document = await responseJson<Record<string, unknown>>(
        await context.ownerApi.post(`/api/agents/${context.agentId}/knowledge/personal/documents`, {
          data: {
            title: `J-07 owner knowledge ${suffix}`,
            markdown: '# Owner knowledge\n\nAtomic source refs and tenant authority.',
            source_kind: 'paste',
            source_uri: `atomic://${sessionId}`,
            agent_searchable: true,
            sensitivity: 'internal',
          },
        }),
        'ingest personal knowledge',
      );
      const documents = await responseJson<Record<string, unknown>>(
        await context.ownerApi.get(`/api/agents/${context.agentId}/knowledge/personal/documents`),
        'list personal knowledge',
      );
      expect(JSON.stringify(documents)).toContain(`J-07 owner knowledge ${suffix}`);
      domain.document = document;
      domain.documents = documents;
      break;
    }
    case 'J-08': {
      const skills = await responseJson<unknown>(await context.ownerApi.get('/api/skills/'), 'list skills');
      const extensions = await responseJson<unknown>(
        await context.ownerApi.get(`/api/agents/${context.agentId}/extensions`),
        'list agent extensions',
      );
      // Typed proof that the model exercised load_skill and the call
      // succeeded. The default user transcript proves the visible tool name
      // and a truthful (non-failed) status; exact call/result linkage pairs
      // top-level invocation_id on the canonical schema_version=2 transcript,
      // where a present payload outcome must be exactly "success".
      const visibleCalls = base.transcript.filter(
        (item) => String(item.item_type) === 'tool_call'
          && String((item.item_data as Record<string, unknown> | undefined)?.tool_name || '') === 'load_skill',
      );
      expect(visibleCalls.length).toBeGreaterThan(0);
      for (const item of visibleCalls) {
        expect(String(item.item_status)).not.toBe('failed');
      }
      const canonical = await responseJson<Array<Record<string, unknown>>>(
        await context.ownerApi.get(
          `/api/agents/${context.agentId}/sessions/${sessionId}/transcript?schema_version=2`,
        ),
        'read canonical V2 transcript',
      );
      const loadSkillCalls = canonical.filter(
        (item) => item.item_kind === 'tool_call'
          && String((item.payload as Record<string, unknown> | undefined)?.tool_name || '') === 'load_skill',
      );
      expect(loadSkillCalls.length).toBeGreaterThan(0);
      const invocationIds = new Set(
        loadSkillCalls.map((item) => String(item.invocation_id || '')).filter(Boolean),
      );
      expect(invocationIds.size).toBe(loadSkillCalls.length);
      for (const item of loadSkillCalls) {
        expect(['failed', 'denied', 'unavailable']).not.toContain(String(item.lifecycle));
      }
      const loadSkillResults = canonical.filter(
        (item) => item.item_kind === 'tool_result' && invocationIds.has(String(item.invocation_id || '')),
      );
      // Exactly one typed result per load_skill call, and the distinct result
      // id set must equal the call id set — a missing or duplicated result
      // for any call fails the journey.
      expect(loadSkillResults.length).toBe(loadSkillCalls.length);
      const resultIds = new Set(loadSkillResults.map((item) => String(item.invocation_id || '')));
      expect(resultIds).toEqual(invocationIds);
      for (const item of loadSkillResults) {
        expect(String((item.payload as Record<string, unknown>).outcome)).toBe('success');
      }
      domain.skills = skills as Record<string, unknown>;
      domain.extensions = extensions as Record<string, unknown>;
      break;
    }
    case 'J-09': {
      const workbench = await responseJson<Record<string, unknown>>(
        await context.ownerApi.get(`/api/agents/${context.agentId}/sessions/${sessionId}/workbench`),
        'read subagent workbench',
      );
      expect(JSON.stringify(base.transcript)).toContain('spawn_subagent');
      expect(JSON.stringify(workbench)).toContain('subagent');
      domain.workbench = workbench;
      break;
    }
    case 'J-10': {
      const team = await responseJson<Record<string, unknown>>(
        await context.ownerApi.post(`/api/agents/${context.agentId}/agent-teams`, {
          data: { parent_session_id: sessionId, name: `J-10 Team ${suffix}` },
        }),
        'create agent team',
      );
      await responseJson(
        await context.ownerApi.post(`/api/agents/${context.agentId}/agent-teams/${team.id}/events`, {
          data: { event_type: 'team_checkpoint', payload: { receipt: 'J-10 controlled checkpoint' } },
        }),
        'record team event',
      );
      const closing = await responseJson<Record<string, unknown>>(
        await context.ownerApi.post(`/api/agents/${context.agentId}/agent-teams/${team.id}/close`, { data: {} }),
        'close team through lead synthesis',
      );
      const workbench = await responseJson<Record<string, unknown>>(
        await context.ownerApi.get(`/api/agents/${context.agentId}/agent-teams/${team.id}/workbench`),
        'read team workbench',
      );
      expect(workbench.schema).toBe('hive.ccplus.agent_team_workbench.v1');
      domain.team = team;
      domain.closing = closing;
      domain.workbench = workbench;
      break;
    }
    case 'J-11': {
      const preview = await responseJson<Record<string, unknown>>(
        await context.ownerApi.post(`/api/agents/${context.agentId}/workflows/preview`, {
          data: {
            definition: {
              name: `J-11 workflow ${suffix}`,
              args_schema: {},
              steps: [{
                id: 'verify',
                type: 'agent_step',
                leaf: { name: 'atomic-verifier', type: 'explorer' },
                task: 'J-11 verify the controlled workflow receipt.',
              }],
            },
            args: {},
            session_id: sessionId,
          },
        }),
        'preview workflow',
      );
      const started = await responseJson<Record<string, unknown>>(
        await context.ownerApi.post(`/api/agents/${context.agentId}/workflows/runs`, {
          data: { preview_id: preview.preview_id },
        }),
        'start workflow',
      );
      let run: Record<string, unknown> = started;
      await expect.poll(async () => {
        run = await responseJson<Record<string, unknown>>(
          await context.ownerApi.get(`/api/agents/${context.agentId}/workflows/runs/${started.run_id}`),
          'poll workflow',
        );
        return String(run.status);
      }, { timeout: 90_000, intervals: [500, 1000] }).toMatch(/completed|failed|cancelled/);
      expect(run.status).toBe('completed');
      domain.preview = preview;
      domain.run = run;
      break;
    }
    case 'J-12': {
      const hr = await responseJson<Record<string, unknown>>(
        await context.ownerApi.get('/api/agents/system/hr'),
        'load system HR agent',
      );
      const hrAgentId = String(hr.id);
      const hrRun = await startAndAwaitChat(context.ownerApi, hrAgentId, 'J-12', { title: 'J-12 HR provisioning' });
      const hrSessionId = String(hrRun.run.session.id);
      const hrRunId = String(hrRun.run.run.run_id);
      const hrCanonical = await responseJson<Array<Record<string, unknown>>>(
        await context.ownerApi.get(`/api/agents/${hrAgentId}/sessions/${hrSessionId}/transcript?schema_version=2`),
        'read HR canonical V2 transcript',
      );
      // Bind the blueprint preview call/result pair to the awaited HR run id
      // and one shared invocation id; only the matched tool_result payload
      // content is parsed. The compatibility ThreadItem projection is never
      // searched for the blueprint id.
      const payloadOf = (item: Record<string, unknown>) => (item.payload as Record<string, unknown> | undefined) || {};
      const boundToHrRun = (item: Record<string, unknown>) => normalizeRunId(canonicalRunId(item)) === normalizeRunId(hrRunId);
      const blueprintCalls = hrCanonical.filter(
        (item) => item.item_kind === 'tool_call' && boundToHrRun(item) && String(payloadOf(item).tool_name || '') === 'preview_agent_blueprint',
      );
      expect(blueprintCalls.length).toBeGreaterThan(0);
      const invocationIds = new Set(blueprintCalls.map((item) => String(item.invocation_id || '')).filter(Boolean));
      expect(invocationIds.size).toBe(blueprintCalls.length);
      const callRowsByInvocation = hrCanonical.filter((item) => item.item_kind === 'tool_call' && boundToHrRun(item));
      for (const invocationId of invocationIds) {
        const invocationRows = callRowsByInvocation.filter((item) => String(item.invocation_id || '') === invocationId);
        expect(invocationRows.some((item) => String(payloadOf(item).outcome || '') === 'success')).toBe(true);
        expect(invocationRows.some((item) => ['failed', 'denied', 'unavailable'].includes(String(payloadOf(item).outcome || '')))).toBe(false);
      }
      const blueprintResults = hrCanonical.filter(
        (item) => item.item_kind === 'tool_result' && boundToHrRun(item) && invocationIds.has(String(item.invocation_id || '')),
      );
      // Exactly one typed result per blueprint call invocation.
      expect(blueprintResults.length).toBe(invocationIds.size);
      expect(new Set(blueprintResults.map((item) => String(item.invocation_id || '')))).toEqual(invocationIds);
      const blueprints = blueprintResults.map((item) => {
        expect(String(payloadOf(item).outcome || '')).toBe('success');
        return JSON.parse(String(payloadOf(item).content || '')) as Record<string, unknown>;
      });
      for (const blueprint of blueprints) {
        expect(UUID_PATTERN.test(String(blueprint.blueprint_id || ''))).toBe(true);
        expect(String(blueprint.session_id || '')).toBe(hrSessionId);
        expect(String(blueprint.hr_agent_id || '')).toBe(hrAgentId);
      }
      const blueprintId = String(blueprints[blueprints.length - 1].blueprint_id);
      const draft = await responseJson<Record<string, unknown>>(
        await context.ownerApi.get(`/api/agents/${hrAgentId}/hr-creation-drafts/${blueprintId}`),
        'read canonical HR draft',
      );
      const confirmed = await responseJson<Record<string, unknown>>(
        await context.ownerApi.post(`/api/agents/${hrAgentId}/hr-creation-drafts/${blueprintId}/confirm`, {
          data: { blueprint_version: draft.blueprint_version, blueprint_hash: draft.blueprint_hash },
        }),
        'confirm canonical HR draft',
      );
      let terminal = confirmed;
      await expect.poll(async () => {
        terminal = await responseJson<Record<string, unknown>>(
          await context.ownerApi.get(`/api/agents/${hrAgentId}/hr-creation-drafts/${blueprintId}`),
          'poll durable HR provisioning',
        );
        return String(terminal.draft_status);
      }, { timeout: 120_000, intervals: [500, 1000] }).toMatch(/completed|failed/);
      expect(terminal.draft_status).toBe('completed');
      domain.draft = draft;
      domain.terminal = terminal;
      return {
        sessionId,
        runId: String(base.run.run.run_id),
        transcript: base.transcript,
        domain,
        browserAgentId: hrAgentId,
        browserSessionId: hrSessionId,
        expectedText: 'Atomic Journey Employee',
      };
    }
    case 'J-13': {
      const signingSecret = 'atomic-signing-secret';
      const config = await responseJson<Record<string, unknown>>(
        await context.ownerApi.post(`/api/agents/${context.agentId}/slack-channel`, {
          data: { bot_token: 'xoxb-atomic', signing_secret: signingSecret },
        }),
        'configure controlled Slack channel',
      );
      const event = {
        type: 'event_callback',
        event_id: `Ev-J13-${suffix}`,
        event: { type: 'message', user: 'U-ATOMIC', channel: 'C-ATOMIC', text: 'J-13 channel ingress delivery', ts: Date.now().toString() },
      };
      const raw = JSON.stringify(event);
      const timestamp = Math.floor(Date.now() / 1000).toString();
      const signature = `v0=${crypto.createHmac('sha256', signingSecret).update(`v0:${timestamp}:${raw}`).digest('hex')}`;
      await responseJson(
        await context.ownerApi.post(`/api/channel/slack/${context.agentId}/webhook`, {
          data: raw,
          headers: {
            'content-type': 'application/json',
            'x-slack-request-timestamp': timestamp,
            'x-slack-signature': signature,
          },
        }),
        'accept signed Slack ingress',
      );
      let external: Record<string, unknown> = {};
      await expect.poll(async () => {
        external = await responseJson<Record<string, unknown>>(await context.fakeApi.get('/evidence'), 'read channel fake evidence');
        const calls = external.calls as Record<string, number>;
        return Number(calls['slack:C-ATOMIC'] || 0);
      }, { timeout: 90_000, intervals: [500, 1000] }).toBeGreaterThan(0);
      domain.channel = config;
      domain.external = external;
      break;
    }
    case 'J-14': {
      const pairing = await responseJson<Record<string, unknown>>(
        await context.ownerApi.post('/api/local-bridge/pairing/init', {
          data: {
            device_name: `J-14 local runner ${suffix}`,
            client_kind: 'hive-connect',
            device_fingerprint: `atomic-${suffix}`,
            scopes: ['workspace:read', 'runtime:execute'],
          },
        }),
        'initialize local bridge pairing',
      );
      await responseJson(
        await context.ownerApi.post(
          `/api/agents/${context.agentId}/local-bridge/pairings/${pairing.user_code}/approve`,
          { data: {} },
        ),
        'approve local bridge pairing',
      );
      const exchange = await responseJson<Record<string, unknown>>(
        await context.ownerApi.post('/api/local-bridge/pairing/exchange', {
          data: { device_code: pairing.device_code },
        }),
        'exchange local bridge device code',
      );
      const status = await responseJson<Record<string, unknown>>(
        await context.ownerApi.get('/api/local-bridge/status', {
          headers: { Authorization: `Bearer ${exchange.access_token}` },
        }),
        'read paired bridge status',
      );
      expect(status.status).toBe('connected');
      domain.pairing = pairing;
      domain.status = status;
      break;
    }
    case 'J-15': {
      const memberRun = await startAndAwaitChat(context.memberApi, context.agentId, 'J-15', { title: 'J-15 audience split' });
      const userProjection = await responseJson<Array<Record<string, unknown>>>(
        await context.memberApi.get(`/api/agents/${context.agentId}/sessions/${memberRun.run.session.id}/transcript`),
        'ordinary audience transcript',
      );
      const operatorProjection = await responseJson<Array<Record<string, unknown>>>(
        await context.ownerApi.get(
          `/api/agents/${context.agentId}/sessions/${memberRun.run.session.id}/transcript?operator_view=true&operator_reason=Atomic%20acceptance`,
        ),
        'operator audience transcript',
      );
      expect(operatorProjection.some((item) => item.operator_details)).toBe(true);
      expect(userProjection.every((item) => !item.operator_details)).toBe(true);
      domain.userProjection = userProjection;
      domain.operatorProjection = operatorProjection;
      return {
        sessionId,
        runId: String(base.run.run.run_id),
        transcript: base.transcript,
        domain,
        browserSessionId: memberRun.run.session.id,
        browserToken: context.member.access_token,
        browserUser: context.member.user,
        expectedText: 'J-15 terminal receipt from the controlled provider.',
      };
    }
    default:
      throw new Error(`Unmapped atomic journey ${journey.id}`);
  }

  return { sessionId, runId: String(base.run.run.run_id), transcript: base.transcript, domain };
}


test.describe.serial('real full-stack atomic user journeys', () => {
  test.beforeAll(async ({ playwright }) => {
    await bootstrap(playwright);
  });

  test.afterAll(async () => {
    await Promise.all([ownerApi, memberApi, intruderApi, fakeApi].filter(Boolean).map((api) => api.dispose()));
  });

  for (const journey of JOURNEYS) {
    test(journey.id + ' ' + journey.name, async ({ page }) => {
      const context: JourneyContext = { ownerApi, memberApi, intruderApi, fakeApi, agentId, owner, member };
      const base = await startAndAwaitChat(ownerApi, agentId, journey.id, {
        receiptOnly: ['J-03', 'J-12', 'J-13'].includes(journey.id),
      });
      const evidence = await exerciseDomain(journey, base, context);
      await expectJourneyEvidence(journey, context, evidence);

      const browserAuth: AuthState = evidence.browserToken
        ? { access_token: evidence.browserToken, user: evidence.browserUser || member.user }
        : owner;
      await setPageAuth(page, browserAuth);
      const browserAgent = evidence.browserAgentId || agentId;
      const browserSession = evidence.browserSessionId || evidence.sessionId;
      await page.goto(`/agents/${browserAgent}?session_id=${browserSession}#chat`, { waitUntil: 'domcontentloaded' });
      await expect(page.getByText(evidence.expectedText || `${journey.id} terminal receipt from the controlled provider.`, { exact: false }).first()).toBeVisible();
    });
  }
});
