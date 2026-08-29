import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const read = (relativePath: string) => readFileSync(new URL(relativePath, import.meta.url), 'utf8');

describe('UX-04 orchestration and composition boundaries', () => {
  it('keeps AgentDetail as a page orchestrator while policy and transport have single owners', () => {
    const source = read('../AgentDetail.tsx');

    expect(source.split('\n').length).toBeLessThanOrEqual(2900);
    expect(source).toContain("from './agent-detail/agentDetailPolicy'");
    expect(source).toContain("from './agent-detail/useSessionTransportController'");
    expect(source).not.toContain('new WebSocket(');
    expect(source).not.toContain('reconnectTimersRef');
  });

  it('routes live and optimistic messages by the exact durable Session identity', () => {
    const source = read('../AgentDetail.tsx');

    expect(source).toContain('appendOptimisticUserMessage(activeRuntimeKey, runSessionId, {');
    expect(source).toContain('setChatMessagesAfterQueuedForSession(sessionId, () => mergePendingForSession(runtimeKey, preParsed))');
    expect(source).not.toContain('(terminal ? setChatMessagesAfterQueued : enqueueChatMessagesUpdate)(');
    expect(source).not.toContain('setChatMessagesAfterQueued(() => mergePendingForSession(runtimeKey');
  });

  it('keeps both canonical and compatibility session evidence behind retryable reads', () => {
    const source = read('../AgentDetail.tsx');

    expect(source).toContain('retrySessionRead(() => chatApi.getSessionTranscript');
    expect(source).toContain('retrySessionRead(() => chatApi.getSessionMessages');
  });

  it('commits the visible list only through the single mixed-plane composition owner', () => {
    const consumerSource = read('./sessionEventConsumer.ts');
    const applierSource = read('./sessionTranscriptApplier.ts');

    // One named owner composes the final visible list across the canonical
    // and compatibility planes (union + identity dedupe + ascending
    // sequence); both hydration and the live applier consume it.
    expect(consumerSource).toContain('export function composeMixedPlaneSessionMessages');
    expect(consumerSource.match(/composeMixedPlaneSessionMessages/g)?.length).toBeGreaterThanOrEqual(2);
    expect(applierSource).toContain('composeMixedPlaneSessionMessages');
    // No plane-specific whole-list replacement of the visible list may
    // remain: neither plane may commit its own replay/projection array as
    // the entire visible list.
    expect(applierSource).not.toContain('next.messages.map(deps.parseChatMsg)');
  });

  it('loads inactive workbench domains on demand and keeps FileBrowser out of the route entry', () => {
    const source = read('../AgentDetail.tsx');

    for (const section of [
      'AgentApprovalsSection',
      'AgentWorkflowsSection',
      'AgentActivityLogSection',
      'AgentAwareSection',
      'AgentEvolutionSection',
      'AgentExtensionsSection',
      'AgentKnowledgeSection',
      'AgentSettingsSection',
      'AgentWorkspaceSection',
      'AgentA2ASection',
      'LocalAgents',
      'LocalAgentChatSection',
    ]) {
      expect(source, section).toContain(`const ${section} = lazy(() => import(`);
    }
    expect(source).not.toContain('OfficeWorkbenchSection');
    expect(source).toContain('<Suspense fallback={<AgentDetailSectionFallback />}');
    expect(source).not.toContain("from '../components/FileBrowser'");
    expect(source).not.toContain('buildNewSkillFilePath');
    expect(source).not.toContain('promptModal');
    expect(source).not.toContain('viewingFile');
  });

  it('enforces the measured AgentDetail route-entry bundle budget in every production build', () => {
    const vite = read('../../../vite.config.ts');
    const packageJson = read('../../../package.json');
    const budget = read('../../../scripts/check-agent-detail-bundle.mjs');

    expect(vite).toContain('manifest: true');
    expect(packageJson).toContain('node scripts/check-agent-detail-bundle.mjs');
    expect(budget).toContain('MAX_AGENT_DETAIL_BYTES = 380_000');
    expect(budget).toContain('MAX_AGENT_DETAIL_GZIP_BYTES = 115_000');
    expect(budget).toContain("dist/.vite/manifest.json");
    expect(budget).toContain("chunk.name === 'AgentDetail'");
  });

  it('composes the chat surface from explicit lineage, artifact, runtime, and tool-result modules', () => {
    const source = read('./AgentChatSection.tsx');

    expect(source.split('\n').length).toBeLessThanOrEqual(2400);
    expect(source).toContain("from './SessionLineageSurface'");
    expect(source).toContain("from './ArtifactSurface'");
    expect(source).toContain("from './SessionRuntimePanel'");
    expect(source).toContain("from './StructuredToolResult'");
  });

  it('keeps extracted surfaces bounded instead of moving the monolith unchanged', () => {
    const limits: Array<[string, number]> = [
      ['./agentDetailPolicy.ts', 420],
      ['./useSessionTransportController.ts', 900],
      ['./SessionLineageSurface.tsx', 700],
      ['./ArtifactSurface.tsx', 500],
      ['./SessionDecisionHistory.tsx', 220],
      ['./SessionRuntimePanel.tsx', 1200],
      ['./StructuredToolResult.tsx', 650],
    ];

    for (const [path, maximum] of limits) {
      expect(read(path).split('\n').length, path).toBeLessThanOrEqual(maximum);
    }
  });

  it('keeps workspace extension tabs as lazy domain owners instead of one admin monolith', () => {
    const orchestrator = read('../workspace/WorkspaceToolsSection.tsx');
    expect(orchestrator.split('\n').length).toBeLessThanOrEqual(180);
    expect(orchestrator).not.toContain('customApiConnectorsApi');
    expect(orchestrator).not.toContain('extensionsApi');
    expect(orchestrator).not.toContain('enterpriseApi');
    expect(orchestrator).not.toContain('toolsApi');
    for (const view of [
      'WorkspaceGlobalToolsView',
      'WorkspaceMcpServersView',
      'WorkspaceCustomApiView',
      'WorkspaceAgentInstalledToolsView',
    ]) {
      expect(orchestrator, view).toContain(`const ${view} = lazy(() => import(`);
    }

    const budgets: Array<[string, number]> = [
      ['../workspace/WorkspaceGlobalToolsView.tsx', 900],
      ['../workspace/WorkspaceCustomApiView.tsx', 320],
      ['../workspace/WorkspaceMcpServersView.tsx', 180],
      ['../workspace/WorkspaceAgentInstalledToolsView.tsx', 150],
      ['../workspace/workspaceToolsModel.tsx', 300],
    ];
    for (const [path, maximum] of budgets) {
      expect(read(path).split('\n').length, path).toBeLessThanOrEqual(maximum);
    }
  });
});
