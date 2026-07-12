import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page, type Route } from '@playwright/test';


const AGENT_ID = '7e57a9e7-0000-4000-8000-000000000028';
type OwnerFailure = 'documents' | 'jobs' | 'graph' | 'grants' | 'proposals' | 'detail' | 'revisions' | 'search' | 'preview';
type AgentFailure = 'documents' | 'detail' | 'search';

const documentSummary = {
  document_id: 'doc-28',
  title: 'Owner evidence notes',
  source_kind: 'paste',
  source_uri: 'browser://knowledge/personal',
  source_sha256: 'a'.repeat(64),
  source_ref: 'kb://person/user-1/documents/doc-28',
  canonical_md_path: 'persons/user-1/kb/doc-28.md',
  status: 'ready',
  sensitivity: 'internal',
  agent_searchable: true,
  segment_count: 1,
  created_at: '2026-07-12T00:00:00Z',
  updated_at: null,
  metadata: {},
};

const documentDetail = {
  ...documentSummary,
  segments: [{
    segment_id: 'segment-28',
    position: 0,
    heading_path: ['Evidence'],
    content: 'Authority failures must never become empty-state claims.',
    token_count: 9,
  }],
};

function deny(route: Route) {
  return route.fulfill({ status: 403, json: { detail: 'Personal Knowledge access denied by owner scope.' } });
}

async function authenticate(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('token', 'personal-kb-e2e-token');
    localStorage.setItem(
      'auth-storage',
      JSON.stringify({
        state: {
          token: 'personal-kb-e2e-token',
          user: { id: 'user-1', username: 'owner', display_name: 'Owner', role: 'admin', tenant_id: 'tenant-1' },
        },
        version: 0,
      }),
    );
  });
}

async function authRoute(route: Route): Promise<boolean> {
  const path = new URL(route.request().url()).pathname;
  if (!path.startsWith('/api/')) {
    await route.fallback();
    return true;
  }
  if (path.endsWith('/auth/me')) {
    await route.fulfill({
      json: {
        id: 'user-1',
        username: 'owner',
        email: 'owner@example.com',
        display_name: 'Owner',
        role: 'admin',
        tenant_id: 'tenant-1',
      },
    });
    return true;
  }
  return false;
}

async function bootstrapOwnerKnowledge(page: Page, options: { failure?: OwnerFailure; empty?: boolean }) {
  await authenticate(page);
  await page.route('**/api/**', async (route) => {
    if (await authRoute(route)) return;
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path === '/api/knowledge/personal/documents' && route.request().method() === 'GET') {
      if (options.failure === 'documents') return deny(route);
      return route.fulfill({ json: { documents: options.empty ? [] : [documentSummary] } });
    }
    if (path === '/api/knowledge/personal/import-jobs') {
      if (options.failure === 'jobs') return deny(route);
      return route.fulfill({ json: { jobs: [] } });
    }
    if (path === '/api/knowledge/personal/graph') {
      if (options.failure === 'graph') return deny(route);
      return route.fulfill({ json: { entities: [], links: [], assertions: [] } });
    }
    if (path === '/api/knowledge/personal/grants') {
      if (options.failure === 'grants') return deny(route);
      return route.fulfill({ json: { grants: [] } });
    }
    if (path === '/api/knowledge/personal/proposals') {
      if (options.failure === 'proposals') return deny(route);
      return route.fulfill({ json: { proposals: [] } });
    }
    if (path === '/api/knowledge/personal/search') {
      if (options.failure === 'search') return deny(route);
      return route.fulfill({ json: { results: [] } });
    }
    if (path === `/api/knowledge/personal/documents/${documentSummary.document_id}/revisions`) {
      if (options.failure === 'revisions') return deny(route);
      return route.fulfill({ json: { revisions: [] } });
    }
    if (path === `/api/knowledge/personal/documents/${documentSummary.document_id}/source-preview`) {
      if (options.failure === 'preview') return deny(route);
      return route.fulfill({ body: 'preview', contentType: 'image/png' });
    }
    if (path === `/api/knowledge/personal/documents/${documentSummary.document_id}`) {
      if (options.failure === 'detail') return deny(route);
      const metadata = options.failure === 'preview'
        ? { media_kind: 'image', source_filename: 'evidence.png', source_mime_type: 'image/png' }
        : {};
      return route.fulfill({ json: { ...documentDetail, metadata } });
    }
    if (route.request().method() === 'GET') return route.fulfill({ json: [] });
    return route.fulfill({ json: {} });
  });
  await page.goto('/knowledge');
}

const ownerFailureJourneys: Array<{
  failure: OwnerFailure;
  activate?: (page: Page) => Promise<void>;
}> = [
  { failure: 'documents' },
  { failure: 'jobs' },
  { failure: 'detail' },
  { failure: 'revisions' },
  { failure: 'preview' },
  { failure: 'graph', activate: (page) => page.getByRole('tab', { name: /Knowledge graph/ }).click() },
  { failure: 'grants', activate: (page) => page.getByRole('tab', { name: /^Grants/ }).click() },
  { failure: 'proposals', activate: (page) => page.getByRole('tab', { name: /Agent proposals/ }).click() },
  {
    failure: 'search',
    activate: async (page) => {
      await page.getByPlaceholder('Search everything you have handled...').fill('authority');
      await page.getByPlaceholder('Search everything you have handled...').press('Enter');
    },
  },
];

for (const journey of ownerFailureJourneys) {
  test(`owner Personal KB exposes ${journey.failure} denial instead of an empty state`, async ({ page }) => {
    await bootstrapOwnerKnowledge(page, { failure: journey.failure });
    if (journey.activate) await journey.activate(page);

    await expect(page.locator('[data-personal-knowledge-state="forbidden"]')).toBeVisible();
    await expect(page.getByText('This is not an empty knowledge base.', { exact: false })).toBeVisible();
    await expect(page.getByText('Personal KB is empty.', { exact: false })).toHaveCount(0);
    if (journey.failure !== 'documents') {
      await expect(page.getByPlaceholder('Search everything you have handled...')).toBeVisible();
    }
    if (journey.failure === 'jobs') {
      await expect(page.getByText('Drag or choose a file', { exact: true })).toBeVisible();
    }
    if (journey.failure === 'revisions') {
      await expect(page.getByText('Authority failures must never become empty-state claims.')).toBeVisible();
    }
    if (journey.failure === 'documents') {
      const accessibility = await new AxeBuilder({ page })
        .include('[data-personal-knowledge-state="forbidden"]')
        .analyze();
      expect(accessibility.violations).toEqual([]);
    }
  });
}

test('owner Personal KB still renders a genuine authorized empty collection as empty', async ({ page }) => {
  await bootstrapOwnerKnowledge(page, { empty: true });
  await page.getByRole('tab', { name: /^Library/ }).click();

  await expect(page.getByText('Personal KB is empty.', { exact: false })).toBeVisible();
  await expect(page.locator('[data-personal-knowledge-state]')).toHaveCount(0);
});

test('retry rechecks authority and recovers from a 403 without fabricating cached emptiness', async ({ page }) => {
  await authenticate(page);
  let denied = true;
  await page.route('**/api/**', async (route) => {
    if (await authRoute(route)) return;
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/knowledge/personal/documents') {
      if (denied) return deny(route);
      return route.fulfill({ json: { documents: [] } });
    }
    if (path === '/api/knowledge/personal/import-jobs') return route.fulfill({ json: { jobs: [] } });
    if (route.request().method() === 'GET') return route.fulfill({ json: [] });
    return route.fulfill({ json: {} });
  });

  await page.goto('/knowledge');
  await expect(page.locator('[data-personal-knowledge-state="forbidden"]')).toBeVisible();
  denied = false;
  await page.getByRole('button', { name: 'Retry' }).click();
  await expect(page.locator('[data-personal-knowledge-state]')).toHaveCount(0);
  await page.getByRole('tab', { name: /^Library/ }).click();
  await expect(page.getByText('Personal KB is empty.', { exact: false })).toBeVisible();
});

const overview = {
  identity: { sections: 0, frozenSections: 0, pendingSoulCandidates: 0, lastUpdated: '' },
  planes: {
    self: { entries: 0, failureModes: { active: 0, mitigating: 0, resolved: 0 } },
    profiles: { entries: 0 },
    knowledge: { pages: 0 },
    milestones: { pages: 0 },
    explicit: { active: 0 },
  },
  pipeline: { pendingPackages: 0, heldJobs: 0, stalled: false },
  growth: {},
  distillers: {
    t2_pipeline: { name: 't2_pipeline', state: 'active', last_run_at: '' },
    heartbeat: { name: 'heartbeat', state: 'active', last_run_at: '' },
    dream: { name: 'dream', state: 'active', last_run_at: '' },
    skillDistiller: { name: 'skill_distiller', state: 'active', last_run_at: '' },
  },
  linkedCapabilities: { skillsReferenced: 0, workflowsReferenced: 0, mcpToolsReferenced: 0, skillCandidates: 0 },
};

async function bootstrapAgentKnowledge(page: Page, failure: AgentFailure) {
  await authenticate(page);
  await page.route('**/api/**', async (route) => {
    if (await authRoute(route)) return;
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path === `/api/agents/${AGENT_ID}` && route.request().method() === 'GET') {
      return route.fulfill({
        json: {
          id: AGENT_ID,
          name: 'Knowledge Agent',
          status: 'idle',
          agent_type: 'native',
          access_level: 'manage',
          role_description: 'Tests governed Personal KB reads.',
        },
      });
    }
    if (path === `/api/agents/${AGENT_ID}/knowledge/overview`) return route.fulfill({ json: overview });
    if (path === `/api/agents/${AGENT_ID}/knowledge/personal/documents`) {
      if (failure === 'documents') return deny(route);
      return route.fulfill({ json: { documents: [documentSummary] } });
    }
    if (path === `/api/agents/${AGENT_ID}/knowledge/personal/search`) {
      if (failure === 'search') return deny(route);
      return route.fulfill({ json: { results: [] } });
    }
    if (path === `/api/agents/${AGENT_ID}/knowledge/personal/documents/${documentSummary.document_id}`) {
      if (failure === 'detail') return deny(route);
      return route.fulfill({ json: documentDetail });
    }
    if (route.request().method() === 'GET') return route.fulfill({ json: [] });
    return route.fulfill({ json: {} });
  });
  await page.goto(`/agents/${AGENT_ID}#knowledge`);
  await page.locator('.agent-knowledge-subviews').getByRole('button', { name: 'Personal KB' }).click();
}

for (const failure of ['documents', 'detail', 'search'] as const) {
  test(`Agent Detail exposes Personal KB ${failure} denial instead of an empty owner scope`, async ({ page }) => {
    await bootstrapAgentKnowledge(page, failure);
    if (failure === 'detail') {
      await page.getByRole('button', { name: /Owner evidence notes/ }).click();
    }
    if (failure === 'search') {
      await page.getByPlaceholder('Search Personal KB...').fill('authority');
      await page.getByRole('button', { name: 'Search', exact: true }).click();
    }

    await expect(page.locator('[data-personal-knowledge-state="forbidden"]')).toBeVisible();
    await expect(page.getByText('This is not an empty knowledge base.', { exact: false })).toBeVisible();
    await expect(page.getByText('Personal KB is empty for this owner scope.')).toHaveCount(0);
  });
}
