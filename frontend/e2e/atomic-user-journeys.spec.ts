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
  anonApi: APIRequestContext;
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
  // External channel sessions only load through the manage-mode operator
  // surface (the product's All-sessions path), never the owner chat shell.
  browserManageMode?: boolean;
  browserOperatorReason?: string;
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
let anonApi: APIRequestContext;
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


async function ensureOperatorInspectionGrant(
  context: JourneyContext,
  principalId: string,
  requestId: string,
): Promise<void> {
  const grant = await responseJson<Record<string, unknown>>(
    await context.ownerApi.post(`/api/agents/${context.agentId}/operator-grants`, {
      data: {
        request_id: requestId,
        principal_id: principalId,
        effect: 'allow',
        reason: 'Atomic scoped operator evidence',
      },
    }),
    'ensure scoped operator inspection grant',
  );
  expect(String(grant.id || '')).toBe(requestId);
  expect(String(grant.principal_id || '')).toBe(principalId);
  expect(String(grant.effect || '')).toBe('allow');
}


async function bootstrap(playwright: PlaywrightWorkerArgs['playwright']): Promise<void> {
  const publicApi = await playwright.request.newContext({ baseURL: HIVE_JOURNEY_BACKEND_URL, timeout: 120_000 });
  const platformAdmin = await registerOrLogin(
    publicApi,
    'atomic_platform_admin',
    'atomic.platform.admin@example.com',
  );
  expect(platformAdmin.user.role).toBe('platform_admin');
  const platformAdminApi = await authContext(playwright, platformAdmin);

  owner = await registerOrLogin(publicApi, 'atomic_owner', 'atomic.owner@example.com');
  if (!owner.user.tenant_id) {
    const company = await responseJson<{ admin_invitation_code: string }>(
      await platformAdminApi.post('/api/admin/companies', {
        data: { name: 'Atomic Journey Tenant' },
      }),
      'create journey company and org-admin invitation',
    );
    const ownerPreTenant = await authContext(playwright, owner);
    const joined = await responseJson<{ access_token: string }>(
      await ownerPreTenant.post('/api/tenants/join', {
        data: { invitation_code: company.admin_invitation_code },
      }),
      'join journey company as org admin',
    );
    await ownerPreTenant.dispose();
    owner = { ...(await login(publicApi, 'atomic_owner')), access_token: joined.access_token };
  }
  expect(owner.user.role).toBe('org_admin');
  await platformAdminApi.dispose();
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
          permission_access_level: 'use',
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
  // The local bridge device flow is genuinely unauthenticated (no JWT, no
  // tenant header) — pairing/init through any authenticated context would
  // mask the anonymous strict-RLS boundary.
  anonApi = await playwright.request.newContext({ baseURL: HIVE_JOURNEY_BACKEND_URL, timeout: 120_000 });
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
  // Run receipts expose the RuntimeTask id as dashless hex; canonical
  // envelopes carry the dashed UUID. Normalize both before binding. Accepted
  // human input uses a SESSION scope by contract, so its run binding lives in
  // payload.legacy_run_id (session_event_contract: V2 reserves the top-level
  // run_id for actual run/round scopes) — read it after the top-level and
  // scope run ids.
  const scope = item.scope as Record<string, unknown> | undefined;
  const payload = (item.payload as Record<string, unknown> | undefined) || {};
  return String(item.run_id || scope?.run_id || payload.legacy_run_id || '');
}

function normalizeRunId(value: string): string {
  // Run receipts expose the RuntimeTask id as dashless hex; canonical
  // envelopes carry the dashed UUID. Normalize both before binding.
  return value.replace(/-/g, '').toLowerCase();
}

function isRunIdToken(value: string): boolean {
  // Runtime task ids surface in both dashed-UUID and dashless-hex forms.
  return /^[0-9a-f]{32}$/.test(normalizeRunId(value));
}

function sortKeysStable(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortKeysStable);
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .sort(([left], [right]) => (left < right ? -1 : left > right ? 1 : 0))
        .map(([key, entry]) => [key, sortKeysStable(entry)]),
    );
  }
  return value;
}

function expectedArgsHash(toolName: string, args: Record<string, unknown>): string {
  // Mirrors backend session_tool_runtime._sha256: canonical JSON with
  // recursively sorted keys, ensure_ascii=False semantics, and compact
  // separators. The schema-v2 tool_call.started payload intentionally
  // exposes tool_name + args_hash instead of raw arguments; hashing the
  // expected inputs proves the exact call inputs without reading private data.
  const encoded = JSON.stringify(sortKeysStable({ tool_name: toolName, arguments: args }));
  return crypto.createHash('sha256').update(encoded, 'utf8').digest('hex');
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

function childSubagentCompatibilityProof(
  envelopes: Array<Record<string, unknown>>,
  journeyReceipt: string,
  subagentTaskId: string,
  childSessionId: string,
  parentSessionId: string,
): boolean {
  // The subagent child session is produced by subagent_run_service, which
  // intentionally persists legacy subagent_task_started/completed events; the
  // schema-v2 read model surfaces them as compatibility envelopes, never as
  // web-chat V2 assistant_text/run_outcome shapes. The strict proof binds the
  // completion envelope to the ONE intended subagent task, child session, and
  // parent session with exact receipt bytes, session contract, and decision
  // entry — and requires exactly two run-bound subagent activity envelopes.
  const taskRunId = normalizeRunId(subagentTaskId);
  const runBound = (item: Record<string, unknown>) => normalizeRunId(String(item.run_id || '')) === taskRunId;
  const isCompatibility = (item: Record<string, unknown>) =>
    String(item.schema || '') === 'hive.session_event_compatibility';
  const subagentRows = envelopes.filter(
    (item) => isCompatibility(item) && runBound(item) && String(item.legacy_event_type || '').startsWith('subagent_task_'),
  );
  if (subagentRows.length !== 2) return false;
  const started = subagentRows.find((item) => String(item.legacy_event_type || '') === 'subagent_task_started');
  const completed = subagentRows.find((item) => String(item.legacy_event_type || '') === 'subagent_task_completed');
  if (!started || !completed) return false;
  if (String(started.legacy_item_status || '') !== 'running') return false;
  if (String(completed.schema_version ?? '') !== '1') return false;
  if (String(completed.compatibility_status || '') !== 'needs_reconciliation') return false;
  if (String(completed.legacy_item_type || '') !== 'subagent_activity') return false;
  if (String(completed.legacy_item_status || '') !== 'succeeded') return false;
  const payload = (completed.payload as Record<string, unknown> | undefined) || {};
  if (String(payload.content || '') !== journeyReceipt) return false;
  const meta = (payload.metadata as Record<string, unknown> | undefined) || {};
  if (String(meta.status || '') !== 'completed') return false;
  if (String(meta.session_state || '') !== 'completed') return false;
  const contract = (meta.session_contract as Record<string, unknown> | undefined) || {};
  if (normalizeRunId(String(contract.run_id || '')) !== taskRunId) return false;
  if (String(contract.continuation_address || '') !== childSessionId) return false;
  const decision = (meta.subagent_decision_entry as Record<string, unknown> | undefined) || {};
  if (normalizeRunId(String(decision.run_id || '')) !== taskRunId) return false;
  if (String(decision.child_session_id || '') !== childSessionId) return false;
  if (String(decision.parent_session_id || '') !== parentSessionId) return false;
  if (String(decision.status || '') !== 'completed') return false;
  if (String(decision.summary || '') !== journeyReceipt) return false;
  return true;
}


async function bridgeReadyHandshake(page: Page, ticket: string): Promise<Record<string, unknown>> {
  // One REAL websocket ready handshake against the production channel WS:
  // resolves on ready_ack (online + effective capabilities) and closes the
  // socket, leaving the bridge offline — the reconnect caller opens a NEW
  // single-use ticket and repeats this handshake.
  const wsUrl = HIVE_JOURNEY_BACKEND_URL.replace(/^http/, 'ws')
    + `/api/local-bridge/channel/ws?ticket=${encodeURIComponent(ticket)}`;
  return await page.evaluate(
    async ({ url }) => {
      return await new Promise<Record<string, unknown>>((resolve, reject) => {
        const socket = new WebSocket(url);
        let settled = false;
        const timer = setTimeout(() => {
          if (settled) return;
          settled = true;
          reject(new Error('bridge ws ready timeout'));
          try {
            socket.close();
          } catch {
            // already closing
          }
        }, 30_000);
        socket.addEventListener('message', (event) => {
          const data = JSON.parse(String(event.data)) as Record<string, unknown>;
          if (data.type === 'ready_ack' && !settled) {
            settled = true;
            clearTimeout(timer);
            socket.close();
            // Resolve only after the CLIENT close event fires so the caller
            // observes a completed disconnect, not a requested one.
            socket.addEventListener('close', () => resolve(data));
          }
        });
        socket.addEventListener('error', () => {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          reject(new Error('bridge ws error'));
        });
        socket.addEventListener('open', () => {
          socket.send(JSON.stringify({
            type: 'ready',
            runtime_kind: 'atomic-harness-runner',
            capabilities: { execute: true, result_report: true },
          }));
        });
      });
    },
    { url: wsUrl },
  );
}


async function startAndAwaitChat(
  api: APIRequestContext,
  currentAgentId: string,
  journeyId: string,
  options: { receiptOnly?: boolean; title?: string; content?: string } = {},
): Promise<{ run: SessionRun; transcript: Array<Record<string, unknown>> }> {
  const content = options.content || `${journeyId} exercise the production journey contract${options.receiptOnly ? ' receipt-only' : ''}.`;
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
): void {
  // Session-wide mechanical closure over the FINAL transcript: collect the
  // union of non-empty invocation ids across ALL tool_call and tool_result
  // rows of every run in the session — the base turn AND every later task
  // (handoff, goal, trigger, workflow, local). Orphan terminal calls, orphan
  // results, duplicate started rows, blank-invocation rows, and ANY terminal
  // failure all fail. For every union member: exactly one started tool_call,
  // exactly one terminal tool_call that is lifecycle completed with payload
  // outcome success, and exactly one tool_result of the same invocation with
  // outcome success. There is no whitelist and no failure exception. Zero
  // tool activity is valid only when the session has none at all.
  const payloadOf = (item: Record<string, unknown>) => (item.payload as Record<string, unknown> | undefined) || {};
  const invocationOf = (item: Record<string, unknown>) => String(item.invocation_id || '');
  const callRows = canonical.filter((item) => item.item_kind === 'tool_call');
  const resultRows = canonical.filter((item) => item.item_kind === 'tool_result');
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
  expectCanonicalSuccessfulToolClosure(canonicalReplay);
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
  page?: Page,
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
      // Exact-byte proof: one unique upload constant, exact projection fields,
      // exactly one matching files item, and a byte-for-byte download through
      // the real download endpoint. No JSON.stringify containment.
      const filename = `j02-${suffix}-deliverable.md`;
      const uploadBytes = `# J-02 deliverable ${suffix}\n\nExact byte payload ${suffix} for the download proof.`;
      const uploaded = await responseJson<Record<string, unknown>>(
        await context.ownerApi.post('/api/chat/upload', {
          multipart: {
            agent_id: context.agentId,
            skip_personal_kb: 'true',
            file: { name: filename, mimeType: 'text/markdown', buffer: Buffer.from(uploadBytes, 'utf8') },
          },
        }),
        'upload deliverable',
      );
      expect(String(uploaded.filename)).toBe(filename);
      expect(String(uploaded.saved_filename || '')).not.toBe('');
      expect(Number(uploaded.size)).toBe(Buffer.byteLength(uploadBytes, 'utf8'));
      expect(String(uploaded.workspace_path)).toBe(`workspace/uploads/${String(uploaded.saved_filename)}`);
      // preview_text is the product's conversion read model: a fixed banner
      // rendered from the conversion block, then the exact uploaded bytes as
      // the Preview body (markdown passthrough preserves content exactly).
      const conversion = (uploaded.conversion as Record<string, unknown> | undefined) || {};
      expect(String(conversion.status)).toBe('converted');
      expect(String(uploaded.preview_text)).toBe(
        `Converted with ${String(conversion.engine)}.\n`
          + `Full Markdown: ${String(conversion.markdown_path)}\n`
          + `Metadata: ${String(conversion.metadata_path)}\n\n`
          + `Preview:\n${uploadBytes}`,
      );
      const files = await responseJson<Array<Record<string, unknown>>>(
        await context.ownerApi.get(`/api/agents/${context.agentId}/files/?path=workspace/uploads`),
        'list workspace deliverables',
      );
      const matching = files.filter(
        (item) => !Boolean(item.is_dir) && String(item.path) === String(uploaded.workspace_path),
      );
      expect(matching).toHaveLength(1);
      expect(String(matching[0].name)).toBe(String(uploaded.saved_filename));
      expect(Number(matching[0].size)).toBe(Buffer.byteLength(uploadBytes, 'utf8'));
      const download = await context.ownerApi.get(
        `/api/agents/${context.agentId}/files/download?path=${encodeURIComponent(String(uploaded.workspace_path))}`,
      );
      expect(download.status()).toBe(200);
      expect(Buffer.from(await download.body()).equals(Buffer.from(uploadBytes, 'utf8'))).toBe(true);
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
      const planVersion = Number(plan.plan_version);
      const planHash = String(plan.plan_hash);
      // The Web PlanCard happy path: confirm and hand off in ONE backend call,
      // then wait for the real execution continuation — "queued" (an active
      // run exists) is not completion and must keep polling.
      const confirmed = await responseJson<Record<string, unknown>>(
        await context.ownerApi.post(`/api/agents/${context.agentId}/plans/${plan.id}/confirm-and-handoff`, {
          data: { plan_version: planVersion, plan_hash: planHash, reason: 'Atomic acceptance' },
        }),
        'confirm and hand off canonical plan',
      );
      expect(String(confirmed.status)).toBe('confirmed');
      let confirmedPlan: Record<string, unknown> = {};
      await expect.poll(async () => {
        confirmedPlan = await responseJson<Record<string, unknown>>(
          await context.ownerApi.get(`/api/agents/${context.agentId}/plans/${plan.id}`),
          'poll plan execution handoff',
        );
        return String(confirmedPlan.handoff_status || '');
      }, { timeout: 90_000, intervals: [500, 1000] }).toBe('completed');
      expect(String(confirmedPlan.status)).toBe('confirmed');
      expect(Number(confirmedPlan.plan_version)).toBe(planVersion);
      expect(String(confirmedPlan.plan_hash)).toBe(planHash);
      const handoffPayload = (confirmedPlan.handoff_payload as Record<string, unknown> | undefined) || {};
      const handoffTaskId = String(handoffPayload.runtime_task_id || '');
      expect(isRunIdToken(handoffTaskId)).toBe(true);
      expect(String(handoffPayload.session_id || '')).toBe(sessionId);
      // The handoff continuation run itself must close terminally in the
      // canonical transcript, bound to that exact runtime task, with the exact
      // J-03 receipt (the plan-execution prompt is answered, never re-planned).
      await expect.poll(async () => {
        const canonical = await responseJson<Array<Record<string, unknown>>>(
          await context.ownerApi.get(
            `/api/agents/${context.agentId}/sessions/${sessionId}/transcript?schema_version=2`,
          ),
          'poll plan handoff terminal proof',
        );
        return hasCanonicalTerminalProof(canonical, 'J-03', handoffTaskId);
      }, { timeout: 90_000, intervals: [500, 1000] }).toBe(true);
      // The handoff EXECUTION run must carry ZERO tool rows — write_file or
      // exit_plan_mode in the continuation would be an unauthorized repeated
      // plan write (the fresh_1855 false green had exactly those four rows).
      const handoffCanonical = await responseJson<Array<Record<string, unknown>>>(
        await context.ownerApi.get(
          `/api/agents/${context.agentId}/sessions/${sessionId}/transcript?schema_version=2`,
        ),
        'read handoff final transcript',
      );
      expect(
        handoffCanonical.filter(
          (item) => ['tool_call', 'tool_result'].includes(String(item.item_kind))
            && normalizeRunId(canonicalRunId(item)) === normalizeRunId(handoffTaskId),
        ),
      ).toHaveLength(0);
      domain.plan = plan;
      domain.confirmed = confirmed;
      domain.confirmedPlan = confirmedPlan;
      domain.handoffTaskId = handoffTaskId;
      break;
    }
    case 'J-04': {
      // The goal must actually RUN: start_immediately with a J-04 marker in
      // the run content, one update_goal completion, terminal goal state, and
      // an idempotent replay that returns the SAME goal and run without a
      // second execution. Goal id equals the request id (insert-keyed).
      const requestId = crypto.randomUUID();
      const goalRunContent = `J-04 exercise the production journey contract with durable goal ${suffix}.`;
      const body = {
        request_id: requestId,
        objective: `J-04 durable goal ${suffix}`,
        token_budget: 4000,
        max_continuation_turns: 2,
        time_budget_seconds: 120,
        content: goalRunContent,
        start_immediately: true,
      };
      const first = await responseJson<Record<string, unknown>>(
        await context.ownerApi.post(`/api/agents/${context.agentId}/sessions/${sessionId}/goals`, { data: body }),
        'start durable goal',
      );
      expect(String(first.id)).toBe(requestId);
      const firstRun = (first.run as Record<string, unknown> | undefined) || {};
      let goalRunId = String(firstRun.run_id || '');
      if (!isRunIdToken(goalRunId)) {
        const inputReceipt = (first.input as Record<string, unknown> | undefined) || {};
        expect(UUID_PATTERN.test(String(inputReceipt.input_id || ''))).toBe(true);
        expect(String(inputReceipt.admission_state || '')).toBe('admitted');
        await expect.poll(async () => {
          const replay = await responseJson<Record<string, unknown>>(
            await context.ownerApi.post(`/api/agents/${context.agentId}/sessions/${sessionId}/goals`, { data: body }),
            'replay deferred goal start',
          );
          const replayRun = (replay.run as Record<string, unknown> | undefined) || {};
          goalRunId = String(replayRun.run_id || '');
          return String(replay.id || '') === requestId && isRunIdToken(goalRunId);
        }, { timeout: 90_000, intervals: [500, 1000] }).toBe(true);
      }
      // The goal run terminates canonically with the exact J-04 receipt.
      await expect.poll(async () => {
        const canonical = await responseJson<Array<Record<string, unknown>>>(
          await context.ownerApi.get(
            `/api/agents/${context.agentId}/sessions/${sessionId}/transcript?schema_version=2`,
          ),
          'poll goal run terminal proof',
        );
        return hasCanonicalTerminalProof(canonical, 'J-04', goalRunId);
      }, { timeout: 90_000, intervals: [500, 1000] }).toBe(true);
      // Exactly ONE update_goal invocation, run-bound to the goal run, with
      // the exact call inputs proven through the args_hash seam.
      const goalCanonical = await responseJson<Array<Record<string, unknown>>>(
        await context.ownerApi.get(`/api/agents/${context.agentId}/sessions/${sessionId}/transcript?schema_version=2`),
        'read goal canonical transcript',
      );
      const goalUpdateCalls = goalCanonical.filter(
        (item) => item.item_kind === 'tool_call'
          && item.lifecycle === 'started'
          && String((item.payload as Record<string, unknown> | undefined)?.tool_name || '') === 'update_goal'
          && normalizeRunId(canonicalRunId(item)) === normalizeRunId(goalRunId),
      );
      expect(goalUpdateCalls).toHaveLength(1);
      expect(String((goalUpdateCalls[0].payload as Record<string, unknown> | undefined)?.args_hash || '')).toBe(
        expectedArgsHash('update_goal', { status: 'complete', summary: 'J-04 durable goal complete.' }),
      );
      // Terminal goal state through the workbench projection.
      const goalProjection = async (): Promise<Record<string, unknown>> => {
        const workbenchNow = await responseJson<Record<string, unknown>>(
          await context.ownerApi.get(
            `/api/agents/${context.agentId}/sessions/${sessionId}/workbench`,
          ),
          'read goal workbench projection',
        );
        return ((workbenchNow.goals as Array<Record<string, unknown>> | undefined) || [])
          .find((goal) => String(goal.id) === requestId) || {};
      };
      await expect.poll(async () => String((await goalProjection()).status || '') === 'complete', {
        timeout: 90_000,
        intervals: [500, 1000],
      }).toBe(true);
      const completedGoal = await goalProjection();
      expect(String(completedGoal.completed_at || '')).not.toBe('');
      expect(String(completedGoal.completion_summary || '')).toBe('J-04 durable goal complete.');
      const snapshot = {
        tokens_used: Number(completedGoal.tokens_used || 0),
        continuation_count: Number(completedGoal.continuation_count || 0),
        token_budget: Number(completedGoal.token_budget || 0),
        max_continuation_turns: Number(completedGoal.max_continuation_turns || 0),
        time_budget_seconds: Number(completedGoal.time_budget_seconds || 0),
      };
      expect(snapshot.token_budget).toBe(4000);
      expect(snapshot.max_continuation_turns).toBe(2);
      expect(snapshot.time_budget_seconds).toBe(120);
      // The Goal API-bound run appears exactly once in the public workbench
      // projection; internal RuntimeTask metadata is deliberately not exposed.
      const goalTasks = (
        (await responseJson<Record<string, unknown>>(
          await context.ownerApi.get(
            `/api/agents/${context.agentId}/sessions/${sessionId}/workbench`,
          ),
          'read goal run workbench',
        )).runtime_tasks as Array<Record<string, unknown>> | undefined) || [];
      expect(
        goalTasks.filter(
          (task) => normalizeRunId(String(task.id || '')) === normalizeRunId(goalRunId),
        ),
      ).toHaveLength(1);
      // Idempotent replay: same body -> same goal id, same run, replayed flag,
      // budgets untouched — id equality alone is never the completion proof.
      const replay = await responseJson<Record<string, unknown>>(
        await context.ownerApi.post(`/api/agents/${context.agentId}/sessions/${sessionId}/goals`, { data: body }),
        'replay durable goal request',
      );
      expect(String(replay.id)).toBe(requestId);
      const replayRun = (replay.run as Record<string, unknown> | undefined) || {};
      expect(String(replayRun.run_id || '')).toBe(goalRunId);
      expect(replayRun.replayed).toBe(true);
      // The replay returns the CANONICAL terminal RuntimeTask status, never
      // the stale write-time snapshot (fresh2_1829 replayed "pending").
      expect(String(replayRun.status || '')).toBe('completed');
      const afterReplay = await goalProjection();
      expect(Number(afterReplay.tokens_used || 0)).toBe(snapshot.tokens_used);
      expect(Number(afterReplay.continuation_count || 0)).toBe(snapshot.continuation_count);
      expect(Number(afterReplay.token_budget || 0)).toBe(4000);
      expect(Number(afterReplay.max_continuation_turns || 0)).toBe(2);
      expect(Number(afterReplay.time_budget_seconds || 0)).toBe(120);
      domain.goal = first;
      domain.replay = replay;
      domain.goalRunId = goalRunId;
      domain.completedGoal = completedGoal;
      break;
    }
    case 'J-05': {
      // Governed one-shot delivery: a DISABLED schedule (unique marker in its
      // instruction), a Plan Mode recommendation that is exactly declined, a
      // manual run queued through the declined-recommendation path, and the
      // real trigger lifecycle to terminal — trigger RuntimeTask completed,
      // the trigger turn's exact receipt in this session, one integration
      // notification bound to the trigger task, the schedule still disabled,
      // and exactly one fire of the one-shot trigger.
      const marker = `j05-${suffix}`;
      const schedule = await responseJson<Record<string, unknown>>(
        await context.ownerApi.post(`/api/agents/${context.agentId}/schedules/`, {
          data: {
            name: `J-05 controlled schedule ${suffix}`,
            instruction: `J-05 exercise the production journey contract with unique marker ${marker}.`,
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
      const recommendation = await responseJson<Record<string, unknown>>(
        await context.ownerApi.post(`/api/agents/${context.agentId}/plan-recommendations`, {
          data: {
            original_request: `J-05 manual schedule run ${suffix}`,
            session_id: sessionId,
            source: 'schedules_api_manual_run',
            title: `J-05 schedule run ${suffix}`,
            intent_type: 'autonomous_wake',
            action_kind: 'create_enabled_trigger',
            tool_name: 'set_trigger',
          },
        }),
        'record plan recommendation',
      );
      const declined = await responseJson<Record<string, unknown>>(
        await context.ownerApi.post(
          `/api/agents/${context.agentId}/plan-recommendations/${recommendation.id}/decline`,
          { data: {} },
        ),
        'decline plan recommendation',
      );
      expect(String(declined.status)).toBe('declined');
      const queued = await responseJson<Record<string, unknown>>(
        await context.ownerApi.post(`/api/agents/${context.agentId}/schedules/${schedule.id}/run`, {
          data: {
            plan_mode_decision: 'declined',
            plan_recommendation_id: String(recommendation.id),
            confirmed_plan_session_id: sessionId,
          },
        }),
        'queue one-shot schedule run',
      );
      expect(String(queued.status)).toBe('queued');
      const triggerId = String(queued.trigger_id || '');
      expect(UUID_PATTERN.test(triggerId)).toBe(true);
      // The REAL one-shot trigger lifecycle: the trigger RuntimeTask reaches
      // terminal completed (discovered through the agent runtime-task read
      // model, bound to this trigger id), fires exactly once, and its trigger
      // session carries the exact receipt plus the integration notification.
      let triggerTaskId = '';
      let triggerSessionId = '';
      await expect.poll(async () => {
        const views = await responseJson<Array<Record<string, unknown>>>(
          await context.ownerApi.get(
            `/api/agents/${context.agentId}/runtime-tasks?task_type=trigger&trigger_id=${triggerId}&diagnostics=true`,
          ),
          'poll trigger runtime task',
        );
        const completed = views.filter((view) => String(view.status || '') === 'completed');
        if (completed.length !== 1) return false;
        triggerTaskId = String(completed[0].task_id || '');
        const diagnostics = (completed[0].diagnostics as Record<string, unknown> | undefined) || {};
        triggerSessionId = String(diagnostics.session_id || '');
        return isRunIdToken(triggerTaskId) && UUID_PATTERN.test(triggerSessionId);
      }, { timeout: 120_000, intervals: [1000, 2000] }).toBe(true);
      // The trigger child session's transcript carries the awakening context
      // and the exact J-05 terminal receipt, BOTH run-bound to the trigger
      // task id. The wake is projected to the TYPED canonical shape: the V1
      // user_message normalizes to item_kind=human_input / lifecycle=accepted
      // / actor.type=user with payload.legacy=true and the run binding in
      // payload.legacy_run_id (accepted input uses a SESSION scope by
      // contract; the top-level run_id is reserved for run/round scopes). The
      // receipt stays a legacy assistant_message envelope with a top-level
      // run_id.
      const readTriggerSession = async (): Promise<Array<Record<string, unknown>>> => responseJson(
        await context.ownerApi.get(
          `/api/agents/${context.agentId}/sessions/${triggerSessionId}/transcript?schema_version=2`,
        ),
        'read trigger child session transcript',
      );
      let triggerSessionCanonical: Array<Record<string, unknown>> = [];
      await expect.poll(async () => {
        triggerSessionCanonical = await readTriggerSession();
        const boundToTriggerTask = (item: Record<string, unknown>) =>
          normalizeRunId(canonicalRunId(item)) === normalizeRunId(triggerTaskId);
        const receiptEnvelope = triggerSessionCanonical.find(
          (item) => boundToTriggerTask(item)
            && String(item.legacy_event_type || '') === 'assistant_message'
            && String((item.payload as Record<string, unknown> | undefined)?.content || '')
              === 'J-05 terminal receipt from the controlled provider.',
        );
        const wakeItem = triggerSessionCanonical.find((item) => {
          if (!boundToTriggerTask(item)) return false;
          if (String(item.item_kind || '') !== 'human_input') return false;
          if (String(item.lifecycle || '') !== 'accepted') return false;
          if (String((item.actor as Record<string, unknown> | undefined)?.type || '') !== 'user') return false;
          const payload = (item.payload as Record<string, unknown> | undefined) || {};
          if (payload.legacy !== true) return false;
          const payloadMetadata = (payload.metadata as Record<string, unknown> | undefined) || {};
          if (String(payloadMetadata.event_type || '') !== 'user_message') return false;
          return String(payload.content || '').includes(marker);
        });
        return Boolean(receiptEnvelope && wakeItem);
      }, { timeout: 30_000, intervals: [500, 1000] }).toBe(true);
      // Exactly ONE integration notification in the child session binds
      // page/outbox/trigger-task/terminal through one manifest item. The
      // transcript is re-read EVERY poll round; the predicate stabilizes only
      // when exactly one matching notification persists.
      await expect.poll(async () => {
        const canonicalNow = await readTriggerSession();
        const matches = canonicalNow.filter((item) => {
          const kind = String(item.legacy_event_type || item.kind || item.event_type || '');
          if (kind !== 'agent_task_notification') return false;
          const payload = (item.payload as Record<string, unknown> | undefined) || {};
          const metadata = (payload.metadata as Record<string, unknown> | undefined) || {};
          if (String(metadata.source || '') !== 'runtime_result_integration') return false;
          const manifest = metadata.result_manifest as Record<string, unknown> | undefined;
          const items = Array.isArray(manifest?.items) ? (manifest.items as Array<Record<string, unknown>>) : [];
          const pageId = String(metadata.integration_page_id || '');
          if (!UUID_PATTERN.test(pageId)) return false;
          if (pageId !== String(metadata.causation_id || '')) return false;
          if (metadata.item_count !== undefined && Number(metadata.item_count) !== 1) return false;
          if (items.length !== 1) return false;
          return items.some(
            (entry) => String(entry.outbox_id || '') === pageId
              && String(entry.source_kind || '') === 'trigger'
              && String(entry.task_type || '') === 'trigger'
              && normalizeRunId(String(entry.source_run_id || '')) === normalizeRunId(triggerTaskId)
              && String(entry.terminal_status || '') === 'completed',
          );
        });
        return matches.length === 1;
      }, { timeout: 30_000, intervals: [500, 1000] }).toBe(true);
      // Child session-wide tool closure: the trigger wake must leave no
      // half-closed tool invocation anywhere in the child session. Re-read
      // once after the notification poll so closure runs on the final stable
      // evidence, not an earlier snapshot.
      triggerSessionCanonical = await readTriggerSession();
      expectCanonicalSuccessfulToolClosure(triggerSessionCanonical);
      // One-shot semantics: exactly one trigger task bound to this trigger id,
      // and the durable trigger row is disabled after firing exactly once.
      const triggerViews = await responseJson<Array<Record<string, unknown>>>(
        await context.ownerApi.get(`/api/agents/${context.agentId}/runtime-tasks?trigger_id=${triggerId}`),
        'list trigger-bound runtime tasks',
      );
      expect(triggerViews).toHaveLength(1);
      const triggers = await responseJson<Array<Record<string, unknown>>>(
        await context.ownerApi.get(`/api/agents/${context.agentId}/triggers`),
        'list agent triggers',
      );
      const triggerRow = triggers.find((row) => String(row.id) === triggerId);
      expect(triggerRow).toBeTruthy();
      expect(Boolean(triggerRow?.is_enabled)).toBe(false);
      expect(Number(triggerRow?.fire_count || 0)).toBe(1);
      expect(Number(triggerRow?.max_fires || 0)).toBe(1);
      // The disabled schedule stays disabled. The legacy activity-log history
      // endpoint is NOT an acceptance source: the modern trigger executor
      // writes no schedule_run/trigger_run activity rows (verified in
      // fresh_2041/fresh_2102), so it carries no run-bound evidence for this
      // path — do not fake one with string matching.
      const schedulesAfter = await responseJson<Array<Record<string, unknown>>>(
        await context.ownerApi.get(`/api/agents/${context.agentId}/schedules/`),
        'reread schedules after trigger',
      );
      const scheduleRow = schedulesAfter.find((item) => item.id === schedule.id);
      expect(scheduleRow).toBeTruthy();
      expect(Boolean(scheduleRow?.is_enabled)).toBe(false);
      domain.schedule = schedule;
      domain.recommendation = recommendation;
      domain.queued = queued;
      domain.triggerTaskId = triggerTaskId;
      domain.triggerSessionId = triggerSessionId;
      // Browser consumption opens the CHILD trigger session — the surface
      // where the one-shot delivery actually landed — for the exact receipt.
      return {
        sessionId,
        runId: String(base.run.run.run_id),
        transcript: base.transcript,
        domain,
        browserSessionId: triggerSessionId,
        expectedText: 'J-05 terminal receipt from the controlled provider.',
      };
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
      const marker = `j07-${suffix}`;
      const document = await responseJson<Record<string, unknown>>(
        await context.ownerApi.post(`/api/agents/${context.agentId}/knowledge/personal/documents`, {
          data: {
            title: `J-07 owner knowledge ${marker}`,
            markdown: `# Owner knowledge ${marker}\n\nUnique per-run marker ${marker}. Atomic source refs and tenant authority.`,
            source_kind: 'paste',
            source_uri: `atomic://${sessionId}`,
            agent_searchable: true,
            sensitivity: 'internal',
          },
        }),
        'ingest personal knowledge',
      );
      // Terminal ingestion through the exact returned job id — completed
      // lifecycle with ready result, never a bare documents list.
      const jobId = String(document.job_id);
      expect(UUID_PATTERN.test(jobId)).toBe(true);
      let job: Record<string, unknown> = {};
      await expect.poll(async () => {
        const jobs = await responseJson<Record<string, unknown>>(
          await context.ownerApi.get(`/api/knowledge/personal/import-jobs`),
          'poll personal import job',
        );
        job = ((jobs.jobs as Array<Record<string, unknown>>) || []).find((row) => String(row.job_id) === jobId) || {};
        return String(job.lifecycle_status || '') === 'completed';
      }, { timeout: 90_000, intervals: [500, 1000] }).toBe(true);
      expect(String(job.result_status)).toBe('ready');
      expect(Number(job.attempt_count || 0)).toBeGreaterThanOrEqual(1);
      // Exact document detail: ready, canonicalized sensitivity, searchable,
      // exact source kind/uri, and an ordered segment carrying the marker.
      const documentId = String(document.document_id);
      const detail = await responseJson<Record<string, unknown>>(
        await context.ownerApi.get(`/api/agents/${context.agentId}/knowledge/personal/documents/${documentId}`),
        'read personal document detail',
      );
      expect(String(detail.status)).toBe('ready');
      expect(String(detail.sensitivity)).toBe('PL1_public');
      expect(detail.agent_searchable).toBe(true);
      expect(String(detail.source_kind)).toBe('paste');
      expect(String(detail.source_uri)).toBe(`atomic://${sessionId}`);
      const detailSegments = (detail.segments as Array<Record<string, unknown>> | undefined) || [];
      expect(detailSegments.length).toBeGreaterThan(0);
      const positions = detailSegments.map((segment) => Number(segment.position || 0));
      expect([...positions].sort((left, right) => left - right)).toEqual(positions);
      expect(detailSegments.some((segment) => String(segment.content || '').includes(marker))).toBe(true);
      // Browser Personal KB search: exact document+segment and the exact
      // kb://person source_ref.
      const browserSearch = await responseJson<Record<string, unknown>>(
        await context.ownerApi.get(`/api/agents/${context.agentId}/knowledge/personal/search?q=${marker}`),
        'browser personal kb search',
      );
      const hits = (browserSearch.results as Array<Record<string, unknown>> | undefined) || [];
      const hit = hits.find((row) => String(row.document_id) === documentId);
      expect(hit).toBeTruthy();
      const hitSegmentId = String((hit as Record<string, unknown>).segment_id);
      expect(String((hit as Record<string, unknown>).source_ref)).toBe(
        `kb://person/${owner.user.id}/documents/${documentId}#segment=${hitSegmentId}`,
      );
      // The second J-07 Agent session must consume the governed progressive-
      // disclosure sequence: tool_search → search_personal_kb(marker) →
      // read_personal_kb(exact ids), all closed canonically by the common
      // closure assertion on its run.
      const second = await startAndAwaitChat(context.ownerApi, context.agentId, 'J-07', {
        title: `J-07 KB consume ${marker}`,
        content: `J-07 exercise the production journey contract with unique marker ${marker}.`,
      });
      const secondRunId = String(second.run.run.run_id);
      const secondSessionId = String(second.run.session.id);
      const secondCanonical = await responseJson<Array<Record<string, unknown>>>(
        await context.ownerApi.get(`/api/agents/${context.agentId}/sessions/${secondSessionId}/transcript?schema_version=2`),
        'read second session canonical transcript',
      );
      // Exact run-bound tool sequence by started order, with exact call inputs
      // proven through the args_hash seam (schema-v2 tool_call.started exposes
      // tool_name + args_hash, never raw arguments).
      const startedCalls = secondCanonical
        .filter(
          (item) => item.item_kind === 'tool_call'
            && item.lifecycle === 'started'
            && normalizeRunId(canonicalRunId(item)) === normalizeRunId(secondRunId),
        )
        .sort((left, right) => Number(left.sequence || 0) - Number(right.sequence || 0));
      expect(
        startedCalls.map((item) => String((item.payload as Record<string, unknown> | undefined)?.tool_name || '')),
      ).toEqual(['tool_search', 'search_personal_kb', 'read_personal_kb']);
      const expectedJ07Calls: Array<[string, Record<string, unknown>]> = [
        ['tool_search', { query: 'personal knowledge' }],
        ['search_personal_kb', { query: marker }],
        ['read_personal_kb', { document_id: documentId, segment_ids: [hitSegmentId] }],
      ];
      expectedJ07Calls.forEach(([toolName, expectedArgs], index) => {
        const payload = (startedCalls[index].payload as Record<string, unknown> | undefined) || {};
        expect(String(payload.args_hash || '')).toBe(expectedArgsHash(toolName, expectedArgs));
      });
      const toolResults = secondCanonical.filter((item) => item.item_kind === 'tool_result');
      const parsedResult = (item: Record<string, unknown>): Record<string, unknown> => {
        const payload = (item.payload as Record<string, unknown> | undefined) || {};
        const content = String(payload.content || '');
        try {
          return JSON.parse(content) as Record<string, unknown>;
        } catch {
          return {};
        }
      };
      const toolCallsByInvocation = secondCanonical.filter((item) => item.item_kind === 'tool_call');
      const toolNameFor = (item: Record<string, unknown>): string => {
        const payload = (item.payload as Record<string, unknown> | undefined) || {};
        return String(payload.tool_name || '');
      };
      const resultForTool = (toolName: string): Record<string, unknown> | undefined => {
        const call = toolCallsByInvocation.find(
          (item) => toolNameFor(item) === toolName
            && normalizeRunId(canonicalRunId(item)) === normalizeRunId(secondRunId),
        );
        if (!call) return undefined;
        return toolResults.find(
          (item) => String(item.invocation_id || '') === String(call.invocation_id || ''),
        );
      };
      const searchResult = resultForTool('search_personal_kb');
      expect(searchResult).toBeTruthy();
      const searchPayload = parsedResult(searchResult as Record<string, unknown>);
      expect(String(searchPayload.status)).toBe('ok');
      expect(((searchPayload.authority as Record<string, unknown>) || {}).allowed).toBe(true);
      const searchHits = (searchPayload.results as Array<Record<string, unknown>> | undefined) || [];
      const matchedHit = searchHits.find((row) => String(row.document_id) === documentId);
      expect(matchedHit).toBeTruthy();
      expect(String((matchedHit as Record<string, unknown>).segment_id)).toBe(hitSegmentId);
      expect(String((matchedHit as Record<string, unknown>).source_ref)).toBe(
        `kb://person/${owner.user.id}/documents/${documentId}#segment=${hitSegmentId}`,
      );
      const readResult = resultForTool('read_personal_kb');
      expect(readResult).toBeTruthy();
      const readPayload = parsedResult(readResult as Record<string, unknown>);
      expect(String(readPayload.status)).toBe('ok');
      expect(String(readPayload.document_id)).toBe(documentId);
      expect(String(readPayload.source_ref)).toBe(`kb://person/${owner.user.id}/documents/${documentId}`);
      expect(((readPayload.authority as Record<string, unknown>) || {}).allowed).toBe(true);
      const readSegments = (readPayload.segments as Array<Record<string, unknown>> | undefined) || [];
      expect(readSegments.some((segment) => String(segment.content || '').includes(marker))).toBe(true);
      expect(hasCanonicalTerminalProof(secondCanonical, 'J-07', secondRunId)).toBe(true);
      domain.document = document;
      domain.job = job;
      domain.detail = detail;
      domain.hit = hit;
      domain.secondRunId = secondRunId;
      return {
        sessionId: secondSessionId,
        runId: secondRunId,
        transcript: second.transcript,
        domain,
        expectedText: 'J-07 terminal receipt from the controlled provider.',
      };
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
      // Mechanical subagent aggregate closure: exactly the completed subagent
      // RuntimeTask bound to this base run and parent session; a valid child
      // session id; the child's compatibility-envelope completion proof bound
      // to that exact subagent task; the integration notification's one
      // manifest item binding page/outbox/subagent/run/child-session/
      // completed; exactly one completed continuation turn bound to the page
      // and base run with the exact J-09 receipt. String-only workbench checks
      // cannot satisfy this.
      await ensureOperatorInspectionGrant(
        context,
        String(context.member.user.id),
        '00000000-0000-4000-8000-000000000091',
      );
      const baseRunId = String(base.run.run.run_id);
      const operatorReason = 'J-09%20subagent%20aggregate%20proof';
      const workbench = await responseJson<Record<string, unknown>>(
        await context.memberApi.get(
          `/api/agents/${context.agentId}/sessions/${sessionId}/workbench?operator_view=true&operator_reason=${operatorReason}`,
        ),
        'read subagent workbench',
      );
      expect(String(workbench.audience || '')).toBe('operator');
      const baseSessionId = String(sessionId);
      let subagentTask: Record<string, unknown> | undefined;
      let childSessionId = '';
      await expect.poll(async () => {
        const workbenchNow = await responseJson<Record<string, unknown>>(
          await context.memberApi.get(
            `/api/agents/${context.agentId}/sessions/${sessionId}/workbench?operator_view=true&operator_reason=${operatorReason}`,
          ),
          'poll subagent task completion',
        );
        const tasks = (workbenchNow.runtime_tasks as Array<Record<string, unknown>> | undefined) || [];
        // Exactly ONE subagent task under this PARENT SESSION, counting every
        // subagent row regardless of root — the fresh_1420 duplicate child was
        // rooted at the continuation, not the base run, so a root-only filter
        // would miss it. The single survivor's root must then normalize to the
        // base run.
        const sessionSubagents = tasks.filter(
          (task) => String(task.task_type || '') === 'subagent'
            && String(((task.metadata as Record<string, unknown> | undefined) || {}).parent_session_id || '') === baseSessionId,
        );
        if (sessionSubagents.length !== 1) return false;
        subagentTask = sessionSubagents[0];
        const taskMetadata = (subagentTask.metadata as Record<string, unknown> | undefined) || {};
        childSessionId = String(taskMetadata.child_session_id || '');
        return String(subagentTask.status || '') === 'completed'
          && normalizeRunId(String(taskMetadata.root_runtime_task_id || '')) === normalizeRunId(baseRunId)
          && UUID_PATTERN.test(childSessionId);
      }, { timeout: 90_000, intervals: [500, 1000] }).toBe(true);
      const subagentTaskId = String((subagentTask as Record<string, unknown>).id);
      // The child session's schema-v2 transcript carries the subagent service's
      // legacy completion rows as compatibility envelopes; the strict proof
      // binds started+completed to exactly this subagent task, child session,
      // and parent session with exact receipt bytes and contract fields.
      const j09Receipt = 'J-09 terminal receipt from the controlled provider.';
      await expect.poll(async () => {
        const childCanonical = await responseJson<Array<Record<string, unknown>>>(
          await context.memberApi.get(
            `/api/agents/${context.agentId}/sessions/${childSessionId}/transcript`
              + `?schema_version=2&operator_view=true&operator_reason=${operatorReason}`,
          ),
          'poll subagent child canonical transcript',
        );
        return childSubagentCompatibilityProof(
          childCanonical,
          j09Receipt,
          subagentTaskId,
          childSessionId,
          baseSessionId,
        );
      }, { timeout: 90_000, intervals: [500, 1000] }).toBe(true);
      // The parent integration notification binds page/outbox/subagent/run/
      // child session/completed through ONE manifest item.
      let integrationPageId = '';
      let notificationSequence = 0;
      await expect.poll(async () => {
        const canonicalNow = await responseJson<Array<Record<string, unknown>>>(
          await context.ownerApi.get(
            `/api/agents/${context.agentId}/sessions/${sessionId}/transcript?schema_version=2`,
          ),
          'poll subagent integration notification',
        );
        const matches = canonicalNow.filter((item) => {
          const kind = String(item.legacy_event_type || item.kind || item.event_type || '');
          if (kind !== 'agent_task_notification') return false;
          const payload = (item.payload as Record<string, unknown> | undefined) || {};
          const metadata = (payload.metadata as Record<string, unknown> | undefined) || {};
          if (String(metadata.source || '') !== 'runtime_result_integration') return false;
          const manifest = metadata.result_manifest as Record<string, unknown> | undefined;
          const items = Array.isArray(manifest?.items) ? (manifest.items as Array<Record<string, unknown>>) : [];
          const pageId = String(metadata.integration_page_id || '');
          if (!UUID_PATTERN.test(pageId)) return false;
          if (pageId !== String(metadata.causation_id || '')) return false;
          if (metadata.item_count !== undefined && Number(metadata.item_count) !== 1) return false;
          if (items.length !== 1) return false;
          const boundItems = items.filter(
            (entry) => String(entry.outbox_id || '') === pageId
              && String(entry.source_kind || '') === 'subagent'
              && String(entry.task_type || '') === 'subagent'
              && normalizeRunId(String(entry.source_run_id || '')) === normalizeRunId(subagentTaskId)
              && String(entry.child_session_id || '') === childSessionId
              && String(entry.terminal_status || '') === 'completed',
          );
          return boundItems.length === 1;
        });
        // Exactly ONE integration notification carries the full manifest
        // binding for this subagent task — a second spawned child must fail,
        // not merely rank behind the first match.
        if (matches.length !== 1) return false;
        integrationPageId = String(
          ((matches[0].payload as Record<string, unknown> | undefined)?.metadata as Record<string, unknown> | undefined)
            ?.integration_page_id || '',
        );
        notificationSequence = Number(matches[0].sequence || 0);
        return true;
      }, { timeout: 90_000, intervals: [500, 1000] }).toBe(true);
      // Exactly one completed continuation turn bound to that page and the
      // base run, distinct from the base run.
      let continuationTaskId = '';
      await expect.poll(async () => {
        const workbenchNow = await responseJson<Record<string, unknown>>(
          await context.memberApi.get(
            `/api/agents/${context.agentId}/sessions/${sessionId}/workbench?operator_view=true&operator_reason=${operatorReason}`,
          ),
          'poll subagent continuation task',
        );
        const tasks = (workbenchNow.runtime_tasks as Array<Record<string, unknown>> | undefined) || [];
        // The API exposes the base run id as dashless hex while task metadata
        // carries dashed UUIDs — compare normalized on both sides.
        const matched = tasks.filter(
          (task) => String(task.task_type || '') === 'web_chat_turn'
            && String(((task.metadata as Record<string, unknown> | undefined) || {}).integration_page_id || '')
              === integrationPageId
            && normalizeRunId(String(((task.metadata as Record<string, unknown> | undefined) || {}).root_runtime_task_id || ''))
              === normalizeRunId(baseRunId)
            && String(task.status || '') === 'completed',
        );
        if (matched.length === 1 && normalizeRunId(String(matched[0].id || '')) !== normalizeRunId(baseRunId)) {
          continuationTaskId = String(matched[0].id);
          return true;
        }
        return false;
      }, { timeout: 90_000, intervals: [500, 1000] }).toBe(true);
      await expect.poll(async () => {
        const canonicalNow = await responseJson<Array<Record<string, unknown>>>(
          await context.ownerApi.get(
            `/api/agents/${context.agentId}/sessions/${sessionId}/transcript?schema_version=2`,
          ),
          'poll continuation receipt snapshot',
        );
        return canonicalNow.some(
          (item) => item.item_kind === 'assistant_text'
            && item.lifecycle === 'snapshot'
            && Number(item.sequence || 0) > notificationSequence
            && normalizeRunId(canonicalRunId(item)) === normalizeRunId(continuationTaskId)
            && String((item.payload as Record<string, unknown> | undefined)?.content || '') === j09Receipt,
        );
      }, { timeout: 90_000, intervals: [500, 1000] }).toBe(true);
      domain.workbench = workbench;
      domain.subagentTaskId = subagentTaskId;
      domain.childSessionId = childSessionId;
      domain.integrationPageId = integrationPageId;
      domain.continuationTaskId = continuationTaskId;
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
      // Aggregate terminal (failing-first): the lead-synthesis continuation
      // must actually close the Team through the delivered close routing —
      // a Team stuck in "closing" can never pass J-10.
      let closedWorkbench: Record<string, unknown> = {};
      await expect.poll(async () => {
        closedWorkbench = await responseJson<Record<string, unknown>>(
          await context.ownerApi.get(`/api/agents/${context.agentId}/agent-teams/${team.id}/workbench`),
          'poll team aggregate terminal close',
        );
        return String(((closedWorkbench.team as Record<string, unknown>) || {}).status || '');
      }, { timeout: 90_000, intervals: [500, 1000] }).toBe('closed');
      const closedTeam = (closedWorkbench.team as Record<string, unknown>) || {};
      expect(String(closedTeam.close_status)).toBe('completed');
      const closeEvents = ((closedWorkbench.events as Array<Record<string, unknown>>) || []).filter(
        (item) => String(item.event_type) === 'team_closed',
      );
      expect(closeEvents).toHaveLength(1);
      const closeEventPayload = (closeEvents[0].payload as Record<string, unknown>) || {};
      expect(UUID_PATTERN.test(String(closeEventPayload.synthesis_run_id || ''))).toBe(true);
      expect(String(closeEventPayload.terminal_status)).toBe('completed');
      domain.team = team;
      domain.closing = closing;
      domain.workbench = closedWorkbench;
      break;
    }
    case 'J-11': {
      await ensureOperatorInspectionGrant(
        context,
        String(context.member.user.id),
        '00000000-0000-4000-8000-000000000091',
      );
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
      // Aggregate closure: the workflow RuntimeTask completing is NOT enough.
      // The canonical notification must be bound to THIS workflow run through
      // its result manifest (source_kind/task_type workflow,
      // source_run_id === started.run_id, terminal item), and the extracted
      // integration page id must be a UUID. Together with the workbench
      // continuation proof below this proves the result was consumed; the
      // fresh-database falsification check (page/outbox status) is what
      // proves the page row itself reached delivered.
      const baseRunId = String(base.run.run.run_id);
      const workflowRunId = String(started.run_id);
      let integrationPageId = '';
      let notificationSequence = 0;
      await expect.poll(async () => {
        const canonicalNow = await responseJson<Array<Record<string, unknown>>>(
          await context.ownerApi.get(
            `/api/agents/${context.agentId}/sessions/${sessionId}/transcript?schema_version=2`,
          ),
          'poll workflow result integration delivery',
        );
        const notification = canonicalNow.find((item) => {
          const kind = String(item.legacy_event_type || item.kind || item.event_type || '');
          if (kind !== 'agent_task_notification') return false;
          const payload = (item.payload as Record<string, unknown> | undefined) || {};
          const metadata = (payload.metadata as Record<string, unknown> | undefined) || {};
          if (String(metadata.source || '') !== 'runtime_result_integration') return false;
          const manifest = metadata.result_manifest as Record<string, unknown> | undefined;
          const items = Array.isArray(manifest?.items) ? (manifest.items as Array<Record<string, unknown>>) : [];
          const pageId = String(metadata.integration_page_id || '');
          if (!UUID_PATTERN.test(pageId)) return false;
          if (pageId !== String(metadata.causation_id || '')) return false;
          // A single manifest item must satisfy every binding at once —
          // outbox row, workflow source, this run, and terminal status — so
          // a multi-result page cannot cross-satisfy from two different items.
          const bound = items.some(
            (entry) => String(entry.outbox_id || '') === pageId
              && String(entry.source_kind || '') === 'workflow'
              && String(entry.task_type || '') === 'workflow'
              && String(entry.source_run_id || '') === workflowRunId
              && String(entry.terminal_status || '') === 'completed',
          );
          if (!bound) return false;
          integrationPageId = pageId;
          notificationSequence = Number(item.sequence || 0);
          return true;
        });
        return Boolean(notification);
      }, { timeout: 90_000, intervals: [500, 1000] }).toBe(true);
      // The manifest's packet step journal, proven from the canonical
      // transcript: workflow_step rows bound to THIS run, exactly one running
      // then exactly one done for step id 'verify', sequences strictly
      // increasing, and both closed BEFORE the terminal integration
      // notification — never just run.status.
      const journalCanonical = await responseJson<Array<Record<string, unknown>>>(
        await context.ownerApi.get(
          `/api/agents/${context.agentId}/sessions/${sessionId}/transcript?schema_version=2`,
        ),
        'read workflow step journal',
      );
      const stepRows = journalCanonical.filter(
        (item) => String(item.legacy_event_type || item.kind || item.event_type || '') === 'workflow_step'
          && normalizeRunId(String(item.run_id || '')) === normalizeRunId(workflowRunId),
      );
      const journal = stepRows
        .map((item) => ({
          sequence: Number(item.sequence || 0),
          status: String(
            ((item.payload as Record<string, unknown> | undefined)?.metadata as Record<string, unknown> | undefined)
              ?.status || '',
          ),
          stepId: String(
            ((item.payload as Record<string, unknown> | undefined)?.metadata as Record<string, unknown> | undefined)
              ?.workflow_step_id || '',
          ),
        }))
        .filter((row) => row.stepId === 'verify')
        .sort((left, right) => left.sequence - right.sequence);
      expect(journal.map((row) => row.status)).toEqual(['running', 'done']);
      expect(journal[0].sequence).toBeLessThan(journal[1].sequence);
      expect(journal[1].sequence).toBeLessThan(notificationSequence);
      // The Session Workbench must show exactly the post-workflow continuation
      // turn bound to this integration page and workflow run — status
      // completed, id distinct from the base turn.
      let continuationTaskId = '';
      await expect.poll(async () => {
        const workbenchNow = await responseJson<Record<string, unknown>>(
          await context.memberApi.get(
            `/api/agents/${context.agentId}/sessions/${sessionId}/workbench`
              + '?operator_view=true&operator_reason=J-11%20workflow%20result%20binding',
          ),
          'poll continuation workbench task',
        );
        // The session_owner audience sanitizes runtime-task metadata away;
        // the binding fields require the explicit operator projection.
        if (String(workbenchNow.audience || '') !== 'operator') return false;
        const tasks = (workbenchNow.runtime_tasks as Array<Record<string, unknown>> | undefined) || [];
        const matched = tasks.filter(
          (task) => String(task.task_type || '') === 'web_chat_turn'
            && String(((task.metadata as Record<string, unknown> | undefined) || {}).integration_page_id || '')
              === integrationPageId
            && String(((task.metadata as Record<string, unknown> | undefined) || {}).root_runtime_task_id || '')
              === workflowRunId
            && String(task.status || '') === 'completed',
        );
        if (matched.length === 1 && String(matched[0].id || '') !== baseRunId) {
          continuationTaskId = String(matched[0].id);
          return true;
        }
        return false;
      }, { timeout: 90_000, intervals: [500, 1000] }).toBe(true);
      // The continuation's receipt snapshot binds to that exact task id.
      await expect.poll(async () => {
        const canonicalNow = await responseJson<Array<Record<string, unknown>>>(
          await context.ownerApi.get(
            `/api/agents/${context.agentId}/sessions/${sessionId}/transcript?schema_version=2`,
          ),
          'poll continuation receipt snapshot',
        );
        return canonicalNow.some(
          (item) => item.item_kind === 'assistant_text'
            && item.lifecycle === 'snapshot'
            && Number(item.sequence || 0) > notificationSequence
            && normalizeRunId(canonicalRunId(item)) === normalizeRunId(continuationTaskId)
            && String((item.payload as Record<string, unknown> | undefined)?.content || '')
              .includes('J-11 terminal receipt from the controlled provider.'),
        );
      }, { timeout: 90_000, intervals: [500, 1000] }).toBe(true);
      domain.preview = preview;
      domain.run = run;
      domain.integrationPageId = integrationPageId;
      domain.continuationTaskId = continuationTaskId;
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
      await ensureOperatorInspectionGrant(
        context,
        String(context.member.user.id),
        '00000000-0000-4000-8000-000000000091',
      );
      const signingSecret = 'atomic-signing-secret';
      const config = await responseJson<Record<string, unknown>>(
        await context.ownerApi.post(`/api/agents/${context.agentId}/slack-channel`, {
          data: { bot_token: 'xoxb-atomic', signing_secret: signingSecret },
        }),
        'configure controlled Slack channel',
      );
      // Snapshot the durable channel-delivery read model and the provider
      // payload baseline before ingress, so only rows and messages created
      // by this journey can satisfy the assertions below.
      const deliveriesBefore = await responseJson<Array<Record<string, unknown>>>(
        await context.ownerApi.get('/api/channel-deliveries?limit=500'),
        'snapshot channel deliveries before ingress',
      );
      const knownIds = new Set(deliveriesBefore.map((row) => String(row.id)));
      const evidenceBefore = await responseJson<Record<string, unknown>>(
        await context.fakeApi.get('/evidence'),
        'snapshot provider evidence before ingress',
      );
      const baselineMessages = (evidenceBefore.slack_messages as Array<Record<string, unknown>> | undefined) || [];
      const baselineMessageCount = baselineMessages.length;
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
      // Terminal delivery: the exact new Slack channel-delivery row for this
      // agent must reach delivered with terminal_result/completed and at
      // least one attempt. The ingress ACK can never satisfy this.
      let delivery: Record<string, unknown> | undefined;
      await expect.poll(async () => {
        const deliveriesNow = await responseJson<Array<Record<string, unknown>>>(
          await context.ownerApi.get('/api/channel-deliveries?limit=500'),
          'poll channel terminal delivery',
        );
        delivery = deliveriesNow.find(
          (row) => !knownIds.has(String(row.id))
            && String(row.agent_id || '') === context.agentId
            && String(row.channel || '') === 'slack'
            && String(row.delivery_kind || '') === 'terminal_result'
            && String(row.terminal_status || '') === 'completed'
            && String(row.status || '') === 'delivered'
            && Number(row.attempt_count || 0) >= 1,
        );
        return Boolean(delivery);
      }, { timeout: 90_000, intervals: [500, 1000] }).toBe(true);
      const deliveryRow = delivery as Record<string, unknown>;
      const externalSessionId = String(deliveryRow.session_id);
      const deliveryRuntimeTaskId = String(deliveryRow.runtime_task_id);
      // The provider must have received the exact terminal model receipt as
      // a NEW message: inspect only messages appended after the pre-ingress
      // count (retry-safe with a persistent provider process) and require
      // exact channel and exact bytes — not a call counter, not includes().
      const exactReceipt = 'J-13 terminal receipt from the controlled provider.';
      let external: Record<string, unknown> = {};
      let deliveredMessage: Record<string, unknown> | undefined;
      await expect.poll(async () => {
        external = await responseJson<Record<string, unknown>>(await context.fakeApi.get('/evidence'), 'read channel fake evidence');
        const messages = (external.slack_messages as Array<Record<string, unknown>> | undefined) || [];
        deliveredMessage = messages
          .slice(baselineMessageCount)
          .find(
            (message) => String(message.channel || '') === 'C-ATOMIC'
              && String(message.text || '') === exactReceipt,
          );
        return Boolean(deliveredMessage);
      }, { timeout: 90_000, intervals: [500, 1000] }).toBe(true);
      // External canonical proof: the delivered row's session must carry the
      // run-bound terminal receipt for exactly the delivery's runtime task,
      // and — per the unbound external principal authority contract — ZERO
      // run-bound tool_call/tool_result rows for that task.
      const externalCanonical = await responseJson<Array<Record<string, unknown>>>(
        await context.memberApi.get(
          `/api/agents/${context.agentId}/sessions/${externalSessionId}/transcript`
            + '?schema_version=2&operator_view=true&operator_reason=J-13%20external%20terminal%20proof',
        ),
        'read external session canonical transcript',
      );
      expect(hasCanonicalTerminalProof(externalCanonical, 'J-13', deliveryRuntimeTaskId)).toBe(true);
      expect(
        externalCanonical.filter(
          (item) => ['tool_call', 'tool_result'].includes(String(item.item_kind || ''))
            && normalizeRunId(canonicalRunId(item)) === normalizeRunId(deliveryRuntimeTaskId),
        ),
      ).toHaveLength(0);
      domain.channel = config;
      domain.external = external;
      domain.delivery = deliveryRow;
      domain.deliveryRuntimeTaskId = deliveryRuntimeTaskId;
      domain.deliveryBeforeCount = deliveriesBefore.length;
      domain.deliveredProviderMessage = deliveredMessage;
      domain.externalCanonical = externalCanonical;
      return {
        sessionId,
        runId: String(base.run.run.run_id),
        transcript: base.transcript,
        domain,
        browserSessionId: externalSessionId,
        browserManageMode: true,
        browserOperatorReason: 'J-13 external terminal proof',
        browserToken: context.member.access_token,
        browserUser: context.member.user,
        expectedText: 'J-13 terminal receipt from the controlled provider.',
      };
    }
    case 'J-14': {
      // The full local bridge production path: explicit local_agent scopes,
      // approval-gated dispatch (waiting_approval -> owner resolves the ONE
      // bound pending approval -> bridge may poll), exactly-one delivery,
      // completed report with a verifiable execution receipt, idempotent
      // report replay, empty re-poll, and exactly one owner-visible result
      // event. Browser consumption points at the local session with the local
      // runner's exact output.
      const bridgeHeaders = (token: string) => ({ Authorization: `Bearer ${token}` });
      const pairing = await responseJson<Record<string, unknown>>(
        await context.anonApi.post('/api/local-bridge/pairing/init', {
          data: {
            device_name: `J-14 local runner ${suffix}`,
            client_kind: 'hive-connect',
            device_fingerprint: `atomic-${suffix}`,
            scopes: ['local_agent:connect', 'local_agent:receive', 'local_agent:send', 'local_agent:report'],
          },
        }),
        'initialize local bridge pairing',
      );
      // Manifest truth: token exchange REQUIRES owner approval first. An
      // anonymous exchange against the still-pending pairing must return the
      // typed pending state with no credentials at all.
      const pendingExchange = await responseJson<Record<string, unknown>>(
        await context.anonApi.post('/api/local-bridge/pairing/exchange', {
          data: { device_code: pairing.device_code },
        }),
        'probe anonymous exchange before approval',
      );
      expect(String(pendingExchange.status)).toBe('pending');
      expect(pendingExchange.access_token).toBeUndefined();
      expect(pendingExchange.connection_id).toBeUndefined();
      const approved = await responseJson<Record<string, unknown>>(
        await context.ownerApi.post(`/api/local-bridge/pairings/${pairing.user_code}/approve`, { data: {} }),
        'approve local bridge pairing',
      );
      const localAgentId = String(approved.agent_id || '');
      expect(UUID_PATTERN.test(localAgentId)).toBe(true);
      const exchange = await responseJson<Record<string, unknown>>(
        await context.anonApi.post('/api/local-bridge/pairing/exchange', {
          data: { device_code: pairing.device_code },
        }),
        'exchange local bridge device code',
      );
      const bridgeToken = String(exchange.access_token || '');
      expect(bridgeToken.startsWith('hb_')).toBe(true);
      const status = await responseJson<Record<string, unknown>>(
        await context.anonApi.get('/api/local-bridge/status', { headers: bridgeHeaders(bridgeToken) }),
        'read paired bridge status',
      );
      expect(String(status.status)).toBe('connected');
      expect(String(status.agent_id || '')).toBe(localAgentId);
      // Initial connect: ready the channel through the production WS path
      // (the only surface that issues signed capability snapshots); the
      // handshake closes the socket, leaving the bridge OFFLINE before the
      // message is dispatched below.
      const firstTicket = await responseJson<Record<string, unknown>>(
        await context.anonApi.post('/api/local-bridge/channel/ws-ticket', {
          headers: bridgeHeaders(bridgeToken),
          data: {},
        }),
        'issue bridge ws ticket',
      );
      const firstReady = await bridgeReadyHandshake(page as Page, String(firstTicket.ticket));
      expect(String(firstReady.status || '')).toBe('online');
      let effective = (firstReady.effective_capabilities as Array<string> | undefined) || [];
      expect(effective).toContain('execute');
      expect(effective).toContain('result_report');
      // Server-observed disconnect: the presence read model must show the
      // bridge OFFLINE before anything is dispatched, so the later offline
      // delivery claim is a real boundary, not a comment.
      await expect.poll(async () => {
        const presenceNow = await responseJson<Record<string, unknown>>(
          await context.anonApi.get('/api/local-bridge/status', { headers: bridgeHeaders(bridgeToken) }),
          'poll bridge presence until offline',
        );
        return String(presenceNow.presence_status || '');
      }, { timeout: 30_000, intervals: [500, 1000] }).toBe('offline');
      const localSession = await responseJson<Record<string, unknown>>(
        await context.ownerApi.post(`/api/agents/${localAgentId}/local-agent/sessions/default`, { data: {} }),
        'create local channel session',
      );
      const localSessionId = String(localSession.id);
      const localChatSessionId = String(localSession.chat_session_id || '');
      expect(UUID_PATTERN.test(localSessionId)).toBe(true);
      expect(UUID_PATTERN.test(localChatSessionId)).toBe(true);
      // The dispatch is approval-gated: waiting_approval + bound pending
      // approval, and the bridge poll is EMPTY until the owner approves.
      const messageBody = {
        content: `J-14 exercise the production journey contract with unique marker j14-${suffix}.`,
        idempotency_key: `j14-${suffix}`,
      };
      const message = await responseJson<Record<string, unknown>>(
        await context.ownerApi.post(
          `/api/agents/${localAgentId}/local-agent/sessions/${localSessionId}/messages`,
          { data: messageBody },
        ),
        'enqueue local runner message',
      );
      const messageId = String(message.id);
      expect(UUID_PATTERN.test(messageId)).toBe(true);
      expect(String(message.status)).toBe('waiting_approval');
      expect(UUID_PATTERN.test(String(message.approval_id || ''))).toBe(true);
      const emptyPoll = await responseJson<Record<string, unknown>>(
        await context.anonApi.get('/api/local-bridge/channel/poll', { headers: bridgeHeaders(bridgeToken) }),
        'poll before approval must be empty',
      );
      expect(((emptyPoll.messages as Array<unknown> | undefined) || [])).toHaveLength(0);
      const approvals = await responseJson<Array<Record<string, unknown>>>(
        await context.ownerApi.get(`/api/agents/${localAgentId}/approvals`),
        'list pending local agent approvals',
      );
      const bound = approvals.filter(
        (row) => String(row.status) === 'pending'
          && String(((row.details as Record<string, unknown> | undefined) || {}).local_agent_message_id || '')
            === messageId,
      );
      expect(bound).toHaveLength(1);
      const resolved = await responseJson<Record<string, unknown>>(
        await context.ownerApi.post(
          `/api/agents/${localAgentId}/approvals/${String(bound[0].id)}/resolve`,
          { data: { action: 'approve' } },
        ),
        'approve local agent dispatch',
      );
      expect(String(resolved.status)).toBe('approved');
      // After the owner action, exactly one delivery reaches the bridge.
      let polled: Array<Record<string, unknown>> = [];
      await expect.poll(async () => {
        const poll = await responseJson<Record<string, unknown>>(
          await context.anonApi.get('/api/local-bridge/channel/poll', { headers: bridgeHeaders(bridgeToken) }),
          'poll local runner messages',
        );
        polled = (poll.messages as Array<Record<string, unknown>> | undefined) || [];
        return polled.length;
      }, { timeout: 30_000, intervals: [500, 1000] }).toBe(1);
      expect(String(polled[0].id)).toBe(messageId);
      expect(String(polled[0].content)).toBe(messageBody.content);
      expect(Number(polled[0].delivery_attempt_count || 0)).toBe(1);
      // Offline -> reconnect recovery: the approved message was DELIVERED
      // through the HTTP poll while the WS was offline; a NEW single-use
      // ticket and a second real ready handshake with the same bridge bearer
      // prove the disconnect/reconnect contract, not just service code.
      const reconnectTicket = await responseJson<Record<string, unknown>>(
        await context.anonApi.post('/api/local-bridge/channel/ws-ticket', {
          headers: bridgeHeaders(bridgeToken),
          data: {},
        }),
        'issue reconnect ws ticket',
      );
      expect(String(reconnectTicket.ticket)).not.toBe(String(firstTicket.ticket));
      const reconnectReady = await bridgeReadyHandshake(page as Page, String(reconnectTicket.ticket));
      expect(String(reconnectReady.status || '')).toBe('online');
      effective = (reconnectReady.effective_capabilities as Array<string> | undefined) || [];
      expect(effective).toContain('execute');
      expect(effective).toContain('result_report');
      // Report the completed local execution with an artifact; the execution
      // receipt must be verifiable and replay-stable.
      const runnerOutput = 'J-14 local runner completed the atomic task.';
      const reportBody = {
        session_id: localSessionId,
        message_id: messageId,
        status: 'completed',
        output: runnerOutput,
        artifacts: [{ path: `workspace/local/j14-${suffix}.txt` }],
      };
      const report = await responseJson<Record<string, unknown>>(
        await context.anonApi.post('/api/local-bridge/channel/report', {
          headers: bridgeHeaders(bridgeToken),
          data: reportBody,
        }),
        'report local runner result',
      );
      expect(String(report.status)).toBe('completed');
      expect(report.idempotent_replay).toBe(false);
      const receipt = (report.receipt as Record<string, unknown> | undefined) || {};
      expect(String(receipt.schema)).toBe('hive.execution_receipt.v1');
      expect(String(receipt.status)).toBe('completed');
      expect(String(receipt.request_hash || '')).not.toBe('');
      expect(String(receipt.capability_snapshot_hash || '')).not.toBe('');
      expect((receipt.result_refs as Array<string> | undefined) || []).toHaveLength(1);
      expect(String(receipt.replay_key || '')).toBe(String(message.replay_key || ''));
      const replayReport = await responseJson<Record<string, unknown>>(
        await context.anonApi.post('/api/local-bridge/channel/report', {
          headers: bridgeHeaders(bridgeToken),
          data: reportBody,
        }),
        'replay local runner result report',
      );
      expect(replayReport.idempotent_replay).toBe(true);
      expect(JSON.stringify(replayReport.receipt)).toBe(JSON.stringify(receipt));
      const repoll = await responseJson<Record<string, unknown>>(
        await context.anonApi.get('/api/local-bridge/channel/poll', { headers: bridgeHeaders(bridgeToken) }),
        're-poll after completion must be empty',
      );
      expect(((repoll.messages as Array<unknown> | undefined) || [])).toHaveLength(0);
      const events = await responseJson<Record<string, unknown>>(
        await context.ownerApi.get(
          `/api/agents/${localAgentId}/local-agent/sessions/${localSessionId}/events`,
        ),
        'read local session events',
      );
      const resultEvents = ((events.events as Array<Record<string, unknown>> | undefined) || [])
        .filter((row) => String(row.type) === 'result' && String(row.message_id || '') === messageId);
      expect(resultEvents).toHaveLength(1);
      expect(String(((resultEvents[0].payload as Record<string, unknown> | undefined) || {}).output || ''))
        .toBe(runnerOutput);
      expect(String(((resultEvents[0].payload as Record<string, unknown> | undefined) || {}).status || ''))
        .toBe('completed');
      // Permission denial on the LIVE path: a second uniquely-keyed message
      // is gated the same way, its bound approval is RESOLVED AS REJECT, and
      // the denial is terminal — no bridge delivery, no result event for the
      // denied message — while the completed main message/result stay
      // exactly one.
      const deniedBody = {
        content: `J-14 denied dispatch probe j14-denied-${suffix}.`,
        idempotency_key: `j14-denied-${suffix}`,
      };
      const deniedMessage = await responseJson<Record<string, unknown>>(
        await context.ownerApi.post(
          `/api/agents/${localAgentId}/local-agent/sessions/${localSessionId}/messages`,
          { data: deniedBody },
        ),
        'enqueue denied local runner message',
      );
      const deniedMessageId = String(deniedMessage.id);
      expect(UUID_PATTERN.test(deniedMessageId)).toBe(true);
      expect(String(deniedMessage.status)).toBe('waiting_approval');
      expect(UUID_PATTERN.test(String(deniedMessage.approval_id || ''))).toBe(true);
      const deniedApprovals = await responseJson<Array<Record<string, unknown>>>(
        await context.ownerApi.get(`/api/agents/${localAgentId}/approvals`),
        'list pending approvals for denied message',
      );
      const deniedBound = deniedApprovals.filter(
        (row) => String(row.status) === 'pending'
          && String(((row.details as Record<string, unknown> | undefined) || {}).local_agent_message_id || '')
            === deniedMessageId,
      );
      expect(deniedBound).toHaveLength(1);
      const deniedResolved = await responseJson<Record<string, unknown>>(
        await context.ownerApi.post(
          `/api/agents/${localAgentId}/approvals/${String(deniedBound[0].id)}/resolve`,
          { data: { action: 'reject' } },
        ),
        'reject local agent dispatch',
      );
      expect(String(deniedResolved.status)).toBe('rejected');
      const deniedPoll = await responseJson<Record<string, unknown>>(
        await context.anonApi.get('/api/local-bridge/channel/poll', { headers: bridgeHeaders(bridgeToken) }),
        'poll after denial must stay empty',
      );
      expect(((deniedPoll.messages as Array<unknown> | undefined) || [])).toHaveLength(0);
      const eventsAfterDenial = await responseJson<Record<string, unknown>>(
        await context.ownerApi.get(
          `/api/agents/${localAgentId}/local-agent/sessions/${localSessionId}/events`,
        ),
        'read local session events after denial',
      );
      const allEvents = (eventsAfterDenial.events as Array<Record<string, unknown>> | undefined) || [];
      expect(
        allEvents.filter((row) => String(row.type) === 'result' && String(row.message_id || '') === deniedMessageId),
      ).toHaveLength(0);
      const deniedResolutionEvents = allEvents.filter(
        (row) => String(row.type) === 'approval_resolved' && String(row.message_id || '') === deniedMessageId,
      );
      expect(deniedResolutionEvents).toHaveLength(1);
      const denialPayload = (deniedResolutionEvents[0].payload as Record<string, unknown> | undefined) || {};
      expect(String(denialPayload.status)).toBe('rejected');
      expect(String(denialPayload.execution_status)).toBe('rejected');
      expect(String(denialPayload.message_status)).toBe('rejected');
      expect(
        allEvents.filter((row) => String(row.type) === 'result' && String(row.message_id || '') === messageId),
      ).toHaveLength(1);

      domain.pairing = pairing;
      domain.localAgentId = localAgentId;
      domain.localSessionId = localSessionId;
      domain.message = message;
      domain.receipt = receipt;
      domain.resultEvent = resultEvents[0];
      domain.deniedMessageId = deniedMessageId;
      return {
        sessionId,
        runId: String(base.run.run.run_id),
        transcript: base.transcript,
        domain,
        // The local channel session belongs to the pairing's default local
        // agent — the browser must open it under THAT agent, not the journey
        // agent, or the UI surfaces another agent's session (404/false green).
        browserAgentId: localAgentId,
        browserSessionId: localChatSessionId,
        expectedText: runnerOutput,
      };
    }
    case 'J-15': {
      // PDEC-013: a scoped business administrator resolves as the real
      // administrator BEFORE the technical inspector branch, so admin business
      // access can never demonstrate the operator lane. The explicit
      // operator.inspect grant must be exercised by an ordinary member reading
      // another principal's Session; their own memberRun Session stays the
      // ordinary user audience (a member's non-operator read of someone else's
      // Session is a 403, not a user projection).
      await ensureOperatorInspectionGrant(
        context,
        String(context.member.user.id),
        '00000000-0000-4000-8000-000000000091',
      );
      const memberRun = await startAndAwaitChat(context.memberApi, context.agentId, 'J-15', { title: 'J-15 audience split' });
      const userProjection = await responseJson<Array<Record<string, unknown>>>(
        await context.memberApi.get(`/api/agents/${context.agentId}/sessions/${memberRun.run.session.id}/transcript`),
        'ordinary audience transcript',
      );
      const operatorProjection = await responseJson<Array<Record<string, unknown>>>(
        await context.memberApi.get(
          `/api/agents/${context.agentId}/sessions/${sessionId}/transcript?operator_view=true&operator_reason=Atomic%20acceptance`,
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
    await Promise.all([ownerApi, memberApi, intruderApi, fakeApi, anonApi].filter(Boolean).map((api) => api.dispose()));
  });

  for (const journey of JOURNEYS) {
    test(journey.id + ' ' + journey.name, async ({ page }) => {
      const context: JourneyContext = { ownerApi, memberApi, intruderApi, fakeApi, anonApi, agentId, owner, member };
      const base = await startAndAwaitChat(ownerApi, agentId, journey.id, {
        receiptOnly: ['J-03', 'J-04', 'J-07', 'J-12', 'J-13', 'J-14'].includes(journey.id),
      });
      const evidence = await exerciseDomain(journey, base, context, page);
      await expectJourneyEvidence(journey, context, evidence);

      const browserAuth: AuthState = evidence.browserToken
        ? { access_token: evidence.browserToken, user: evidence.browserUser || member.user }
        : owner;
      await setPageAuth(page, browserAuth);
      const browserAgent = evidence.browserAgentId || agentId;
      const browserSession = evidence.browserSessionId || evidence.sessionId;
      await page.goto(
        `/agents/${browserAgent}?session_id=${browserSession}${evidence.browserManageMode ? '&manage' : ''}#chat`,
        { waitUntil: 'domcontentloaded' },
      );
      if (evidence.browserOperatorReason) {
        await page.getByLabel('Operator inspection reason').fill(evidence.browserOperatorReason);
        await page.getByTestId('agent-operator-reason').getByRole('button', { name: 'Begin inspection' }).click();
        await expect(page.getByTestId('session-operator-view')).toBeVisible();
      }
      await expect(page.getByText(evidence.expectedText || `${journey.id} terminal receipt from the controlled provider.`, { exact: false }).first()).toBeVisible();
    });
  }
});
