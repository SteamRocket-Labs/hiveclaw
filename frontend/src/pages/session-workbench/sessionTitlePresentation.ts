const BRANCH_MODES = new Set([
  'branch',
  'fork',
  'rewind',
  'edit',
  'insert_before',
  'insert_after',
  'reply',
  'regenerate',
  'side_question',
]);

const LEGACY_BRANCH_TITLE_SUFFIX = /\s+\((?:branch|fork|edit|insert|reply|regenerate|btw|clear)\)\s*$/i;

function recordValue(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

export function sessionBranchMode(session: Record<string, unknown> | null | undefined): string {
  const source = recordValue(session);
  const branch = recordValue(source.branch);
  const metadata = recordValue(source.transcript_metadata_json || source.metadata_json || source.metadata);
  return String(
    source.branch_mode
      || branch.branch_mode
      || branch.mode
      || metadata.branch_mode
      || metadata.mode
      || '',
  ).trim().toLowerCase();
}

export function hasSessionBranchIdentity(session: Record<string, unknown> | null | undefined): boolean {
  const source = recordValue(session);
  if (BRANCH_MODES.has(sessionBranchMode(source))) return true;
  const id = source.id == null ? '' : String(source.id);
  const parentId = source.parent_session_id == null ? '' : String(source.parent_session_id);
  const rootId = source.root_session_id == null ? '' : String(source.root_session_id);
  return Boolean(parentId && parentId !== id) || Boolean(rootId && rootId !== id && parentId);
}

export function sessionTitleForUser(
  session: Record<string, unknown> | null | undefined,
  fallback: string,
): string {
  const source = recordValue(session);
  const rawTitle = String(source.title || source.name || '').trim() || fallback;
  if (!hasSessionBranchIdentity(source)) return rawTitle;

  let title = rawTitle;
  while (LEGACY_BRANCH_TITLE_SUFFIX.test(title)) {
    title = title.replace(LEGACY_BRANCH_TITLE_SUFFIX, '').trim();
  }
  return title || fallback;
}
