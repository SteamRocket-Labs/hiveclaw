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

async function bootstrapOwnerKnowledge(page: Page, options: { failure?: OwnerFailure; empty?: boolean; jobs?: unknown[]; theme?: string }) {
  await authenticate(page);
  if (options.theme) {
    await page.addInitScript((theme) => {
      localStorage.setItem('theme', theme);
    }, options.theme);
  }
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
      return route.fulfill({ json: { jobs: options.jobs ?? [] } });
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
      await page.getByPlaceholder('Search your documents and notes...').fill('authority');
      await page.getByPlaceholder('Search your documents and notes...').press('Enter');
    },
  },
];

for (const journey of ownerFailureJourneys) {
  test(`owner Personal KB exposes ${journey.failure} denial instead of an empty state`, async ({ page }) => {
    await bootstrapOwnerKnowledge(page, { failure: journey.failure });
    if (journey.activate) await journey.activate(page);

    await expect(page.locator('[data-personal-knowledge-state="forbidden"]')).toBeVisible();
    await expect(page.getByText('This is not an empty knowledge base.', { exact: false })).toBeVisible();
    // The denial copy speaks plainly: no "Owner scope" jargon.
    await expect(page.getByText('Owner scope', { exact: false })).toHaveCount(0);
    await expect(page.getByText('Nothing here yet.', { exact: false })).toHaveCount(0);
    if (journey.failure !== 'documents') {
      await expect(page.getByPlaceholder('Search your documents and notes...')).toBeVisible();
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

  await expect(page.getByText('Nothing here yet.', { exact: false })).toBeVisible();
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
  await expect(page.getByText('Nothing here yet.', { exact: false })).toBeVisible();
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
  memoryStatus: {
    state: 'consolidating',
    availableForRecall: true,
    recentMemoryAvailable: true,
    longTermMemoryAvailable: true,
    pendingConsolidation: true,
    pendingItems: 2,
    issueCount: 0,
  },
  growth: {},
  distillers: {
    t2_pipeline: { name: 't2_pipeline', state: 'active', last_run_at: '' },
    heartbeat: { name: 'heartbeat', state: 'active', last_run_at: '' },
    dream: { name: 'dream', state: 'active', last_run_at: '' },
    skillDistiller: { name: 'skill_distiller', state: 'active', last_run_at: '' },
  },
  linkedCapabilities: { skillsReferenced: 0, workflowsReferenced: 0, mcpToolsReferenced: 0, skillCandidates: 0 },
};

async function bootstrapAgentKnowledge(page: Page, failure: AgentFailure | null, openPersonal = true) {
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
  if (openPersonal) {
    await page.locator('.agent-knowledge-subviews').getByRole('button', { name: 'Personal KB' }).click();
  }
}

test('Agent Detail exposes memory readiness without internal lifecycle names', async ({ page }) => {
  await bootstrapAgentKnowledge(page, null, false);

  const overviewGrid = page.locator('.agent-knowledge-overview-grid');
  await expect(overviewGrid.getByText('Consolidating', { exact: true })).toBeVisible();
  await expect(
    overviewGrid.getByText('Recent experiences are available for future conversations', { exact: false }),
  ).toBeVisible();
  await expect(overviewGrid.getByText('Available for future conversations: Available', { exact: true })).toBeVisible();
  await expect(overviewGrid.getByText('Long-term memory: Consolidated', { exact: true })).toBeVisible();
  await expect(overviewGrid.getByText('Organizing 2 recent experiences', { exact: true })).toBeVisible();
  await expect(overviewGrid).not.toContainText('T0→T2');
  await expect(overviewGrid).not.toContainText('Heartbeat');
  await expect(overviewGrid).not.toContainText('Dream');
  await expect(overviewGrid).not.toContainText('runtime_task_id');

  const accessibility = await new AxeBuilder({ page }).include('.agent-knowledge-overview-grid').analyze();
  expect(accessibility.violations).toEqual([]);
});

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

// ---------------------------------------------------------------------------
// KNOWLEDGE-NOVICE-DISCLOSURE-001: media import failure recovery, keyboard
// access, and readable first-screen layouts (mock harness, no real backend).
// ---------------------------------------------------------------------------

const mediaImportJob = {
  job_id: 'job-media-1',
  document_id: 'doc-media-1',
  stage: 'transcribing',
  status: 'failed',
  artifact_hash: 'b'.repeat(64),
  error_message: 'unsupported_or_unconfigured:media_transcription_provider',
  attempt_count: 1,
  metadata: { source_filename: 'voice-memo.mp3', media_kind: 'audio', source_mime_type: 'audio/mpeg' },
  created_at: '2026-09-01T00:00:00Z',
  updated_at: '2026-09-01T00:01:00Z',
  terminal: true,
  retryable: true,
  cancellable: false,
  error_code: 'unsupported_or_unconfigured',
  max_attempts: 5,
  lifecycle_status: 'failed',
  result_status: 'failed',
  cancelled_at: null,
};

test('retryable media failure explains its prerequisites and sends exactly one retry request', async ({ page }) => {
  await bootstrapOwnerKnowledge(page, { empty: true, jobs: [mediaImportJob] });

  await expect(page.getByText('voice-memo.mp3')).toBeVisible();
  // The explanation names both prerequisites; the raw machine code and the
  // provider detail never reach the DOM.
  await expect(page.getByText('administrator may need to enable media processing', { exact: false })).toBeVisible();
  await expect(page.getByText('plain-text version instead', { exact: false })).toBeVisible();
  await expect(page.getByText('unsupported_or_unconfigured', { exact: false })).toHaveCount(0);
  await expect(page.getByText('media_transcription_provider', { exact: false })).toHaveCount(0);

  let retryRequests = 0;
  let jobListRequests = 0;
  // The retry success path selects the requeued document; the mock owes the
  // detail endpoint a truthful body for it (the generic catch-all {} would
  // not be a real backend response).
  await page.route(`**/api/knowledge/personal/documents/${mediaImportJob.document_id}`, async (route) => {
    await route.fulfill({
      json: {
        document_id: mediaImportJob.document_id,
        title: 'voice-memo.mp3',
        source_kind: 'upload',
        source_uri: null,
        source_sha256: 'b'.repeat(64),
        source_ref: 'kb://person/user-1/documents/doc-media-1',
        canonical_md_path: 'persons/user-1/kb/doc-media-1.md',
        status: 'queued',
        sensitivity: 'internal',
        agent_searchable: true,
        segment_count: 0,
        created_at: '2026-09-01T00:00:00Z',
        updated_at: null,
        metadata: { media_kind: 'audio', source_filename: 'voice-memo.mp3' },
        segments: [],
      },
    });
  });
  await page.route('**/api/knowledge/personal/import-jobs/job-media-1/retry', async (route) => {
    retryRequests += 1;
    await route.fulfill({
      json: { ...mediaImportJob, lifecycle_status: 'queued', status: 'queued', retryable: false, error_code: null, error_message: null },
    });
  });
  await page.route('**/api/knowledge/personal/import-jobs', async (route) => {
    if (route.request().method() === 'GET') {
      jobListRequests += 1;
      return route.fulfill({ json: { jobs: [mediaImportJob] } });
    }
    return route.fallback();
  });

  await page.getByRole('button', { name: 'Retry' }).click();
  await expect.poll(() => retryRequests).toBe(1);
  // The success path refreshes the authoritative job list: the re-registered
  // route sees exactly one follow-up GET (the initial page load predates it).
  // Even after that refresh settles, exactly one retry request was sent.
  await expect.poll(() => jobListRequests).toBe(1);
  expect(retryRequests).toBe(1);
  await expect(page.locator('.personal-kb-job-row').getByText('voice-memo.mp3')).toBeVisible();
});

test('non-retryable media failure shows the explanation without a retry action', async ({ page }) => {
  await bootstrapOwnerKnowledge(page, {
    empty: true,
    jobs: [{ ...mediaImportJob, retryable: false, attempt_count: 5 }],
  });

  await expect(page.getByText('voice-memo.mp3')).toBeVisible();
  await expect(page.getByText('administrator may need to enable media processing', { exact: false })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Retry' })).toHaveCount(0);
});

// Measures the effective WCAG contrast of a control's rendered focus
// indicator (outline ring or focus shadow ring) against the nearest opaque
// surface behind it. This fails when the indicator is missing, unparsable,
// or below any ratio we assert — it never trusts the global rule's mere
// presence.
async function measureFocusIndicator(
  locator: ReturnType<Page['locator']>,
  channel: 'outline' | 'shadow',
) {
  return locator.evaluate((element, indicatorChannel) => {
    const parseColor = (value: string): { r: number; g: number; b: number; a: number } | null => {
      const match = value.match(/rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)/);
      if (!match) return null;
      return {
        r: Number(match[1]),
        g: Number(match[2]),
        b: Number(match[3]),
        a: match[4] === undefined ? 1 : Number(match[4]),
      };
    };
    let backdrop = { r: 255, g: 255, b: 255, a: 1 };
    let node: HTMLElement | null = element;
    while (node) {
      const background = parseColor(getComputedStyle(node).backgroundColor);
      if (background && background.a === 1) {
        backdrop = background;
        break;
      }
      node = node.parentElement;
    }
    const style = getComputedStyle(element);
    const raw = indicatorChannel === 'outline' ? style.outlineColor : style.boxShadow;
    const indicator = parseColor(raw);
    const luminance = (color: { r: number; g: number; b: number }) => {
      const channelValue = (v: number) => {
        const s = v / 255;
        return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
      };
      return 0.2126 * channelValue(color.r) + 0.7152 * channelValue(color.g) + 0.0722 * channelValue(color.b);
    };
    if (!indicator) {
      return {
        ratio: 0,
        raw,
        backdrop: `rgb(${backdrop.r}, ${backdrop.g}, ${backdrop.b})`,
        outlineStyle: style.outlineStyle,
        matchesFocusVisible: element.matches(':focus-visible'),
      };
    }
    const effective = indicator.a >= 1
      ? indicator
      : {
        r: indicator.r * indicator.a + backdrop.r * (1 - indicator.a),
        g: indicator.g * indicator.a + backdrop.g * (1 - indicator.a),
        b: indicator.b * indicator.a + backdrop.b * (1 - indicator.a),
        a: 1,
      };
    const lighter = Math.max(luminance(effective), luminance(backdrop));
    const darker = Math.min(luminance(effective), luminance(backdrop));
    return {
      ratio: (lighter + 0.05) / (darker + 0.05),
      raw,
      backdrop: `rgb(${backdrop.r}, ${backdrop.g}, ${backdrop.b})`,
      outlineStyle: style.outlineStyle,
      matchesFocusVisible: element.matches(':focus-visible'),
    };
  }, channel);
}

test('lane tabs are keyboard operable with a visible, high-contrast focus indicator', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 720 });
  await bootstrapOwnerKnowledge(page, { empty: true, theme: 'light' });

  // Real keyboard flow: Tab backwards from the search input lands on the last
  // lane tab, which must show a rendered focus indicator whose effective
  // color holds at least 3:1 against the surface behind it (WCAG 2.1 SC
  // 1.4.11 non-text contrast).
  await page.getByPlaceholder('Search your documents and notes...').click();
  await page.keyboard.press('Shift+Tab');
  const grantsTab = page.getByRole('tab', { name: /^Grants/ });
  await expect(grantsTab).toBeFocused();
  const focusIndicator = await measureFocusIndicator(grantsTab, 'outline');
  expect(focusIndicator.matchesFocusVisible).toBe(true);
  expect(focusIndicator.outlineStyle).toBe('solid');
  expect(focusIndicator.ratio).toBeGreaterThanOrEqual(3);

  // Enter activates the focused lane; the denial-safe empty state appears.
  await page.keyboard.press('Enter');
  await expect(grantsTab).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByText('No additional grants yet.', { exact: false })).toBeVisible();

  // Grant sharing is described truthfully, in plain language: Agent grants
  // expire; a grant to a person stays until revoked. No "everything expires"
  // promise and no "you can always read everything" promise.
  await expect(page.getByText('Agent grants always expire', { exact: false })).toBeVisible();
  await expect(page.getByText('stays until you revoke it', { exact: false })).toBeVisible();
  await expect(page.getByText('always read and change everything', { exact: false })).toHaveCount(0);
  const ceilingOptions = page.locator('select[aria-label="Sensitivity ceiling"] option');
  await expect(ceilingOptions).toHaveText(['Public', 'Personal details', 'Sensitive', 'Credentials (reference only)']);
  await expect(ceilingOptions.nth(3)).toHaveAttribute('value', 'PL4_credential');
  const purposeOptions = page.locator('select[aria-label="Grant purpose"] option');
  await expect(purposeOptions).toHaveText(['Runs on its own', 'In a session with you', 'Delegated to another Agent', 'Delegated to a sub-Agent']);
  await expect(purposeOptions.first()).toHaveAttribute('value', 'autonomous_agent');

  // Form fields render focus as a shadow ring; on this page that ring must
  // meet the same 3:1 floor, measured on the effective computed color.
  await page.getByPlaceholder('Agent or user ID').focus();
  const fieldRing = await measureFocusIndicator(page.getByPlaceholder('Agent or user ID'), 'shadow');
  expect(fieldRing.ratio).toBeGreaterThanOrEqual(3);
});

test('the lane focus indicator also holds 3:1 in the dark theme', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 720 });
  await bootstrapOwnerKnowledge(page, { empty: true, theme: 'dark' });

  await page.getByPlaceholder('Search your documents and notes...').click();
  await page.keyboard.press('Shift+Tab');
  const grantsTab = page.getByRole('tab', { name: /^Grants/ });
  await expect(grantsTab).toBeFocused();
  const focusIndicator = await measureFocusIndicator(grantsTab, 'outline');
  expect(focusIndicator.matchesFocusVisible).toBe(true);
  expect(focusIndicator.outlineStyle).toBe('solid');
  expect(focusIndicator.ratio).toBeGreaterThanOrEqual(3);
});

test('lane tab strip keeps one desktop row and full narrow/phone visibility', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 720 });
  await bootstrapOwnerKnowledge(page, { empty: true });
  const strip = page.locator('.personal-kb-tabs');
  await expect(page.getByRole('tab', { name: /^Grants/ })).toBeVisible();

  // Desktop: all six lanes stay on a single row (47px design height; the
  // threshold still cleanly separates one row from the two-row 101px state).
  const desktopHeight = await strip.evaluate((element) => element.getBoundingClientRect().height);
  expect(desktopHeight).toBeLessThanOrEqual(54);

  // Narrow and phone: every lane control must be fully visible with zero
  // horizontal overflow and no clipped text — a sliver or a 1px scrollbar is
  // not a navigation contract for a novice. The compact-sidebar collapse is
  // matchMedia-driven and animated, so wait for the settled layout (poll the
  // visibility count) before taking each measurement.
  const measureStrip = () => strip.evaluate((element) => {
    const stripRect = element.getBoundingClientRect();
    const tabs = Array.from(element.querySelectorAll<HTMLElement>('.personal-kb-tab'));
    const fullyVisible = tabs.filter((tab) => {
      const rect = tab.getBoundingClientRect();
      return rect.left >= stripRect.left - 0.5 && rect.right <= stripRect.right + 0.5
        && rect.top >= stripRect.top - 0.5 && rect.bottom <= stripRect.bottom + 0.5;
    }).length;
    const clippedText = tabs.filter((tab) => (
      Array.from(tab.querySelectorAll<HTMLElement>('strong, small'))
        .some((node) => node.scrollWidth > node.clientWidth + 1)
    )).length;
    return { fullyVisible, clippedText, hiddenWidth: element.scrollWidth - element.clientWidth, tabCount: tabs.length };
  });

  const stripSettled = async () => {
    const m = await measureStrip();
    return m.fullyVisible === 6 && m.hiddenWidth === 0 && m.clippedText === 0;
  };

  await page.setViewportSize({ width: 800, height: 720 });
  await expect(page.locator('.app-layout')).toHaveClass(/sidebar-collapsed/);
  await expect.poll(stripSettled).toBe(true);
  const narrow = await measureStrip();
  expect(narrow.tabCount).toBe(6);
  expect(narrow.hiddenWidth).toBe(0);
  expect(narrow.clippedText).toBe(0);

  await page.setViewportSize({ width: 390, height: 720 });
  await expect.poll(stripSettled).toBe(true);
  const phone = await measureStrip();
  expect(phone.tabCount).toBe(6);
  expect(phone.hiddenWidth).toBe(0);
  expect(phone.clippedText).toBe(0);

  // Every lane stays keyboard-reachable in the same DOM order.
  await page.getByRole('tab', { name: /^Inbox/ }).focus();
  for (let step = 0; step < 5; step += 1) await page.keyboard.press('Tab');
  await expect(page.getByRole('tab', { name: /^Grants/ })).toBeFocused();
});

const noviceScreenshotCases = [
  { name: 'knowledge-novice-first-screen-light-desktop.png', theme: 'light', viewport: { width: 1280, height: 720 } },
  { name: 'knowledge-novice-first-screen-dark-desktop.png', theme: 'dark', viewport: { width: 1280, height: 720 } },
  { name: 'knowledge-novice-first-screen-light-narrow.png', theme: 'light', viewport: { width: 800, height: 720 } },
] as const;

for (const screenshotCase of noviceScreenshotCases) {
  test(`novice first screen stays readable: ${screenshotCase.theme} ${screenshotCase.viewport.width}px`, async ({ page }) => {
    await page.setViewportSize(screenshotCase.viewport);
    await bootstrapOwnerKnowledge(page, { empty: true, jobs: [mediaImportJob], theme: screenshotCase.theme });
    await expect(page.getByText('voice-memo.mp3')).toBeVisible();
    await expect(page.getByText('administrator may need to enable media processing', { exact: false })).toBeVisible();
    // First-screen novice actions stay reachable in every layout.
    await expect(page.getByPlaceholder('Search your documents and notes...')).toBeVisible();
    await expect(page).toHaveScreenshot(screenshotCase.name);
  });
}
