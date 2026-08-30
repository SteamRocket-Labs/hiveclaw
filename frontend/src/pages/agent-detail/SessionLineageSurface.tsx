import React from 'react';
import { useTranslation } from 'react-i18next';
import { sessionTitleForUser } from '../session-workbench/sessionTitlePresentation';

export interface BranchLineageItem {
  id: string;
  parent_session_id?: string | null;
  root_session_id?: string | null;
  title?: string | null;
  branch?: Record<string, unknown> | null;
  created_at?: string | null;
}

export interface BranchLineageRow extends BranchLineageItem {
  depth: number;
}

export function branchModeLabel(item: BranchLineageItem): string {
  const branch = item.branch || {};
  const mode = String(branch.branch_mode || branch.mode || '').trim();
  if (!mode) return 'root';
  if (mode === 'rewind') return 'legacy rewind branch';
  return mode.replace(/_/g, ' ');
}

export function buildBranchLineageRows(lineage: BranchLineageItem[]): BranchLineageRow[] {
  const byId = new Map(lineage.map((item) => [String(item.id), item]));
  const children = new Map<string, BranchLineageItem[]>();
  const roots: BranchLineageItem[] = [];
  lineage.forEach((item) => {
    const parentId = item.parent_session_id ? String(item.parent_session_id) : null;
    if (!parentId || !byId.has(parentId)) {
      roots.push(item);
      return;
    }
    const list = children.get(parentId) || [];
    list.push(item);
    children.set(parentId, list);
  });

  const rows: BranchLineageRow[] = [];
  const seen = new Set<string>();
  const visit = (item: BranchLineageItem, depth: number) => {
    const id = String(item.id);
    if (seen.has(id)) return;
    seen.add(id);
    rows.push({ ...item, depth });
    (children.get(id) || []).forEach((child) => visit(child, depth + 1));
  };
  roots.forEach((root) => visit(root, 0));
  lineage.forEach((item) => {
    if (!seen.has(String(item.id))) visit(item, 0);
  });
  return rows;
}

export function BranchLineagePanel({
  activeSessionId,
  lineage,
  loading = false,
  onSelectSession,
}: {
  activeSessionId?: string | null;
  lineage: BranchLineageItem[];
  loading?: boolean;
  onSelectSession: (sessionId: string) => void | Promise<unknown>;
}) {
  const { t } = useTranslation();
  if (loading) {
    return (
      <div data-testid="branch-lineage-panel" style={{ padding: '8px 12px', borderBottom: '1px solid var(--border-subtle)', fontSize: '11px', color: 'var(--text-tertiary)' }}>
        {t('common.loading', 'Loading')}
      </div>
    );
  }
  if (lineage.length <= 1) return null;
  const rows = buildBranchLineageRows(lineage);
  return (
    <div data-testid="branch-lineage-panel" style={{ padding: '8px 12px', borderBottom: '1px solid var(--border-subtle)' }}>
      <div style={{ fontSize: '10px', color: 'var(--text-tertiary)', textTransform: 'uppercase', letterSpacing: 0, marginBottom: '6px' }}>
        {t('agent.chat.branch.branches', 'Branches')}
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
        {rows.map((row) => {
          const isActive = String(row.id) === String(activeSessionId || '');
          return (
            <button
              key={row.id}
              type="button"
              data-testid="branch-lineage-row"
              onClick={() => onSelectSession(String(row.id))}
              style={{
                border: 'none',
                background: isActive ? 'var(--bg-secondary)' : 'transparent',
                color: isActive ? 'var(--text-primary)' : 'var(--text-secondary)',
                borderRadius: '4px',
                cursor: 'pointer',
                padding: '4px 6px',
                paddingLeft: `${6 + row.depth * 12}px`,
                textAlign: 'left',
                fontSize: '11px',
                lineHeight: 1.3,
              }}
            >
              <span style={{ color: 'var(--text-tertiary)', marginRight: '5px' }}>{branchModeLabel(row)}</span>
              <span>{sessionTitleForUser(row as unknown as Record<string, unknown>, row.id)}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

export function sessionCheckpointId(checkpoint: Record<string, unknown>): string {
  return String(
    checkpoint.checkpoint_event_id
      || checkpoint.event_id
      || checkpoint.message_id
      || checkpoint.id
      || '',
  );
}

export function sessionCheckpointSequence(checkpoint: Record<string, unknown>): string {
  const metadata = checkpoint.metadata && typeof checkpoint.metadata === 'object' && !Array.isArray(checkpoint.metadata)
    ? checkpoint.metadata as Record<string, unknown>
    : {};
  return String(
    checkpoint.sequence
      ?? checkpoint.turn_index
      ?? metadata.sequence
      ?? metadata.turn_index
      ?? '',
  );
}

export function branchAnchorSequence(item: BranchLineageItem): string {
  const branch = item.branch || {};
  return String(
    branch.anchor_sequence
      ?? branch.checkpoint_sequence
      ?? branch.sequence
      ?? branch.turn_index
      ?? '',
  );
}

export function resolveBranchAnchorCheckpointId(
  item: BranchLineageItem,
  checkpoints: Array<Record<string, unknown>>,
  checkpointIds: Set<string>,
): string {
  const directAnchorId = branchAnchorId(item);
  if (directAnchorId && checkpointIds.has(directAnchorId)) return directAnchorId;

  const anchorSequence = branchAnchorSequence(item);
  if (!anchorSequence) return directAnchorId;

  const matchedCheckpoint = checkpoints.find((checkpoint) => (
    sessionCheckpointSequence(checkpoint) === anchorSequence
  ));
  return matchedCheckpoint ? sessionCheckpointId(matchedCheckpoint) : directAnchorId;
}

export type SessionScrollCheckpointAnchor = {
  id: string;
  top: number;
};

export function pickFocusedCheckpointIdForScroll(
  anchors: SessionScrollCheckpointAnchor[],
  viewportCenterY: number,
): string | null {
  const sorted = anchors
    .filter((anchor) => anchor.id && Number.isFinite(anchor.top))
    .sort((a, b) => a.top - b.top);
  if (sorted.length === 0) return null;

  let focused = sorted[0];
  for (const anchor of sorted) {
    if (anchor.top > viewportCenterY) break;
    focused = anchor;
  }
  return focused.id;
}

export type SessionGitLineDensity = 'empty' | 'sparse' | 'regular' | 'scrollable';

export const SESSION_CHECKPOINT_PREVIEW_MAX_CHARS = 22;

export const SESSION_CHECKPOINT_PREVIEW_ELLIPSIS = '...';

export function getSessionGitLineDensity(itemCount: number): SessionGitLineDensity {
  if (itemCount <= 0) return 'empty';
  if (itemCount <= 6) return 'sparse';
  if (itemCount >= 32) return 'scrollable';
  return 'regular';
}

export function checkpointTextValue(value: unknown): string {
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) {
    return value
      .map((item) => {
        if (typeof item === 'string') return item;
        if (item && typeof item === 'object') {
          const part = item as Record<string, unknown>;
          return checkpointTextValue(part.text ?? part.content ?? part.value);
        }
        return '';
      })
      .filter(Boolean)
      .join(' ');
  }
  return '';
}

export function normalizeCheckpointPreviewText(value: unknown): string {
  return checkpointTextValue(value).replace(/\s+/g, ' ').trim();
}

export function isOpaqueCheckpointPreview(text: string, checkpoint: Record<string, unknown>): boolean {
  if (!text) return true;
  const id = sessionCheckpointId(checkpoint);
  if (id && text === id) return true;
  if (/^[a-z0-9_.:-]{16,}$/i.test(text) && !/\s/.test(text)) return true;
  return false;
}

export function checkpointPromptIntent(checkpoint: Record<string, unknown>): string {
  const metadata = checkpoint.metadata && typeof checkpoint.metadata === 'object'
    ? checkpoint.metadata as Record<string, unknown>
    : {};
  const candidates = [
    checkpoint.prompt,
    checkpoint.user_prompt,
    checkpoint.original_prompt,
    checkpoint.input,
    checkpoint.query,
    checkpoint.request,
    checkpoint.content_preview,
    checkpoint.display_content,
    checkpoint.content,
    metadata.prompt,
    metadata.user_prompt,
    metadata.original_prompt,
    metadata.display_content,
    metadata.content_preview,
    metadata.llm_content,
    metadata.user_message,
    checkpoint.title,
    checkpoint.summary,
  ];
  for (const candidate of candidates) {
    const text = normalizeCheckpointPreviewText(candidate);
    if (!isOpaqueCheckpointPreview(text, checkpoint)) return text;
  }
  return '';
}

export function sessionCheckpointPreview(checkpoint: Record<string, unknown>, index: number): string {
  const intent = checkpointPromptIntent(checkpoint) || `Checkpoint ${index + 1}`;
  if (intent.length <= SESSION_CHECKPOINT_PREVIEW_MAX_CHARS) return intent;
  return `${intent.slice(0, SESSION_CHECKPOINT_PREVIEW_MAX_CHARS - SESSION_CHECKPOINT_PREVIEW_ELLIPSIS.length).trimEnd()}${SESSION_CHECKPOINT_PREVIEW_ELLIPSIS}`;
}

export function branchAnchorId(item: BranchLineageItem): string {
  const branch = item.branch || {};
  return String(
    branch.anchor_event_id
      || branch.checkpoint_event_id
      || branch.event_id
      || branch.message_id
      || '',
  );
}

export function SessionGitLine({
  activeSessionId,
  axisSessionId,
  checkpoints,
  focusedCheckpointId,
  lineage,
  loading,
  rewindAnchorCheckpointId,
  onNavigateCheckpoint,
  onNavigateBranch,
}: {
  activeSessionId?: string | null;
  axisSessionId?: string | null;
  checkpoints: Array<Record<string, unknown>>;
  focusedCheckpointId?: string | null;
  lineage: BranchLineageItem[];
  loading?: boolean;
  rewindAnchorCheckpointId?: string | null;
  onNavigateCheckpoint: (checkpoint: Record<string, unknown>, index: number) => void;
  onNavigateBranch?: (sessionId: string) => void | Promise<unknown>;
}) {
  const { t } = useTranslation();
  const navigationLabel = t('sessionWorkbench.gitLine.navigation', 'Session navigation');
  const lineageRows = lineage.length > 1 ? buildBranchLineageRows(lineage) : [];
  const rootRow = lineageRows.find((row) => row.depth === 0) || null;
  const branchRows = lineageRows.filter((row) => row.depth > 0);
  const checkpointIds = new Set(checkpoints.map((checkpoint) => sessionCheckpointId(checkpoint)).filter(Boolean));
  const branchesByAnchorId = new Map<string, BranchLineageRow[]>();
  const orphanBranchRows: BranchLineageRow[] = [];
  branchRows.forEach((row) => {
    const anchorId = resolveBranchAnchorCheckpointId(row, checkpoints, checkpointIds);
    if (!anchorId || !checkpointIds.has(anchorId)) {
      orphanBranchRows.push(row);
      return;
    }
    const rows = branchesByAnchorId.get(anchorId) || [];
    rows.push(row);
    branchesByAnchorId.set(anchorId, rows);
  });
  // Rewound tail (soft projection): checkpoints after the rewind anchor stay
  // navigable but render dimmed — the transcript keeps them, the head moved.
  const rewindAnchorIndex = rewindAnchorCheckpointId
    ? checkpoints.findIndex((checkpoint) => sessionCheckpointId(checkpoint) === rewindAnchorCheckpointId)
    : -1;
  const density = getSessionGitLineDensity(checkpoints.length + branchRows.length);
  const densityClass = `is-${density}`;
  const [expandedBranchAnchorId, setExpandedBranchAnchorId] = React.useState<string | null>(null);
  const [hoverPreview, setHoverPreview] = React.useState<{
    label: string;
    meta: string[];
    top: number;
    left: number;
  } | null>(null);
  const openPreview = React.useCallback((
    event: React.MouseEvent<HTMLButtonElement> | React.FocusEvent<HTMLButtonElement>,
    label: string,
    meta: string[] = [],
  ) => {
    const rect = event.currentTarget.getBoundingClientRect();
    setHoverPreview({
      label,
      meta,
      top: rect.top + rect.height / 2,
      left: rect.right + 8,
    });
  }, []);
  const closePreview = React.useCallback(() => {
    setHoverPreview(null);
  }, []);
  const renderBranchButton = (row: BranchLineageRow, compact = false, resolvedAnchorId?: string) => {
    const isActive = String(row.id) === String(activeSessionId || '');
    const title = sessionTitleForUser(row as unknown as Record<string, unknown>, row.id);
    const anchorId = resolvedAnchorId || resolveBranchAnchorCheckpointId(row, checkpoints, checkpointIds);
    const previewLabel = `${branchModeLabel(row)} · ${title}`;
    return (
      <button
        key={row.id}
        type="button"
        data-testid="session-gitline-branch"
        data-session-action="navigate-branch"
        data-branch-session-id={row.id}
        data-checkpoint-id={anchorId || undefined}
        className={`session-gitline-branch-node ${compact ? 'is-compact' : ''} ${isActive ? 'is-active' : ''}`}
        title={previewLabel}
        aria-label={previewLabel}
        onMouseEnter={(event) => openPreview(event, previewLabel)}
        onFocus={(event) => openPreview(event, previewLabel)}
        onMouseLeave={closePreview}
        onBlur={closePreview}
        onClick={() => onNavigateBranch?.(String(row.id))}
      >
        <span className="session-gitline-branch-stem" aria-hidden="true" />
        <span className="session-gitline-branch-dot" aria-hidden="true" />
      </button>
    );
  };
  const activeBranchRow = branchRows.find((row) => String(row.id) === String(activeSessionId || '')) || null;
  const activeBranchAnchorId = activeBranchRow
    ? resolveBranchAnchorCheckpointId(activeBranchRow, checkpoints, checkpointIds)
    : '';
  const canReturnToRoot = Boolean(rootRow && activeSessionId && String(rootRow.id) !== String(activeSessionId));
  const renderRootReturnButton = (anchorId?: string) => {
    if (!rootRow || !canReturnToRoot) return null;
    const title = sessionTitleForUser(rootRow as unknown as Record<string, unknown>, rootRow.id);
    const previewLabel = `${t('agent.chat.branch.mainSession', 'Main session')} · ${title}`;
    return (
      <button
        key={`root-${rootRow.id}`}
        type="button"
        data-testid="session-gitline-root"
        data-session-action="navigate-root-session"
        data-branch-session-id={rootRow.id}
        data-checkpoint-id={anchorId || undefined}
        className="session-gitline-branch-node session-gitline-root-node"
        title={previewLabel}
        aria-label={previewLabel}
        onMouseEnter={(event) => openPreview(event, previewLabel)}
        onFocus={(event) => openPreview(event, previewLabel)}
        onMouseLeave={closePreview}
        onBlur={closePreview}
        onClick={() => onNavigateBranch?.(String(rootRow.id))}
      >
        <span className="session-gitline-branch-stem" aria-hidden="true" />
        <span className="session-gitline-branch-dot" aria-hidden="true" />
      </button>
    );
  };
  if (loading && checkpoints.length === 0 && branchRows.length === 0) {
    return (
      <aside
        data-testid="session-gitline"
        data-axis-session-id={axisSessionId || undefined}
        data-active-session-id={activeSessionId || undefined}
        data-density={density}
        className={`session-gitline is-loading ${densityClass}`}
        aria-label={navigationLabel}
      >
        <span className="session-gitline-loading-dot" />
      </aside>
    );
  }
  if (checkpoints.length === 0 && branchRows.length === 0) {
    return (
      <aside
        data-testid="session-gitline"
        data-axis-session-id={axisSessionId || undefined}
        data-active-session-id={activeSessionId || undefined}
        data-density={density}
        className={`session-gitline is-empty ${densityClass}`}
        aria-label={navigationLabel}
      />
    );
  }
  return (
    <aside
      data-testid="session-gitline"
      data-axis-session-id={axisSessionId || undefined}
      data-active-session-id={activeSessionId || undefined}
      data-density={density}
      className={`session-gitline ${densityClass}`}
      aria-label={navigationLabel}
    >
      <div className="session-gitline-track" data-testid="session-gitline-checkpoints">
        {checkpoints.map((checkpoint, index) => {
          const id = sessionCheckpointId(checkpoint);
          const isFocused = Boolean(id && id === focusedCheckpointId);
          const previewLabel = sessionCheckpointPreview(checkpoint, index);
          const isRewoundTail = rewindAnchorIndex >= 0 && index > rewindAnchorIndex;
          const nodeState = isRewoundTail
            ? 'rewound_tail'
            : rewindAnchorIndex >= 0 && index === rewindAnchorIndex
              ? 'current_head'
              : 'past';
          const previewMeta = [
            String((checkpoint as Record<string, unknown>).created_at || '').slice(0, 16),
            isRewoundTail ? t('agent.chat.gitline.rewoundTail', 'rewound — head moved before this point') : '',
          ].filter(Boolean) as string[];
          const anchoredBranches = id ? branchesByAnchorId.get(id) || [] : [];
          const hasActiveBranch = anchoredBranches.some((row) => String(row.id) === String(activeSessionId || ''));
          const isExpandedBranchAnchor = Boolean(id && expandedBranchAnchorId === id);
          const rootReturnNode = id && canReturnToRoot && activeBranchAnchorId === id
            ? renderRootReturnButton(id)
            : null;
          const showCluster = anchoredBranches.length > 2 && !isExpandedBranchAnchor;
          const branchClusterLabel = anchoredBranches
            .map((row) => row.title || row.id)
            .slice(0, 4)
            .join(' · ');
          return (
            <div
              key={id || index}
              className={`session-gitline-node-wrap ${anchoredBranches.length ? 'has-branches' : ''} ${hasActiveBranch ? 'has-active-branch' : ''}`}
            >
              <button
                type="button"
                data-testid="session-gitline-checkpoint"
                data-session-action="navigate-checkpoint"
                data-checkpoint-id={id || undefined}
                className={`session-gitline-node ${isFocused ? 'is-focused' : ''} ${isRewoundTail ? 'is-rewound-tail' : ''}`}
                data-state={nodeState}
                title={previewLabel}
                aria-label={previewLabel}
                onMouseEnter={(event) => openPreview(event, previewLabel, previewMeta)}
                onFocus={(event) => openPreview(event, previewLabel, previewMeta)}
                onMouseLeave={closePreview}
                onBlur={closePreview}
                onClick={() => onNavigateCheckpoint(checkpoint, index)}
              />
              {(anchoredBranches.length > 0 || rootReturnNode) && (
                <div className={`session-gitline-branch-group ${isExpandedBranchAnchor ? 'is-expanded' : ''}`}>
                  {rootReturnNode}
                  {showCluster ? (
                    <button
                      type="button"
                      data-testid="session-gitline-branch-cluster"
                      data-session-action="expand-branches"
                      data-checkpoint-id={id || undefined}
                      className={`session-gitline-branch-cluster ${hasActiveBranch ? 'is-active' : ''}`}
                      title={branchClusterLabel}
                      aria-label={branchClusterLabel || t('agent.chat.branch.branches', 'Branches')}
                      onMouseEnter={(event) => openPreview(
                        event,
                        t('agent.chat.branch.branches', 'Branches'),
                        anchoredBranches.map((row) => `${branchModeLabel(row)} · ${row.title || row.id}`),
                      )}
                      onFocus={(event) => openPreview(
                        event,
                        t('agent.chat.branch.branches', 'Branches'),
                        anchoredBranches.map((row) => `${branchModeLabel(row)} · ${row.title || row.id}`),
                      )}
                      onMouseLeave={closePreview}
                      onBlur={closePreview}
                      onClick={(event) => {
                        event.preventDefault();
                        event.stopPropagation();
                        setExpandedBranchAnchorId(id || null);
                      }}
                    >
                      +{anchoredBranches.length}
                    </button>
                  ) : (
                    anchoredBranches.map((row) => renderBranchButton(row, isExpandedBranchAnchor, id || undefined))
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
      {orphanBranchRows.length > 0 && (
        <div className="session-gitline-branches" data-testid="session-gitline-branches">
          {activeBranchRow && orphanBranchRows.some((row) => String(row.id) === String(activeSessionId || ''))
            ? renderRootReturnButton(activeBranchAnchorId || undefined)
            : null}
          {orphanBranchRows.map((row) => renderBranchButton(row))}
        </div>
      )}
      {hoverPreview ? (
        <div
          className="session-gitline-preview session-gitline-hovercard"
          style={{ top: `${hoverPreview.top}px`, left: `${hoverPreview.left}px` }}
          role="tooltip"
          data-testid="session-gitline-hovercard"
        >
          <strong>{hoverPreview.label}</strong>
          {hoverPreview.meta.map((line, index) => (
            <div key={index}>{line}</div>
          ))}
        </div>
      ) : null}
    </aside>
  );
}
