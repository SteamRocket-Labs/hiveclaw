import { assistantNameForDisplay } from '../assistant/assistantScope';

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

const ATTACHMENT_MARKER_RE = /^\[📎\s*([^\]]+)\]\s*/;

export type AttachmentMarkerStripping = {
  /** The text after the leading `[📎 file]` markers, whitespace trimmed. */
  text: string;
  /** The last marker's file name, when a marker was present. */
  fileName: string;
};

/** Drop leading `[📎 file]` markers (the chip already shows the file) while
 * preserving the rest of the text. */
export function dropLeadingAttachmentMarkers(value: string): AttachmentMarkerStripping {
  let cleaned = String(value || '');
  let fileName = '';
  for (;;) {
    const marker = cleaned.match(ATTACHMENT_MARKER_RE);
    if (!marker) break;
    fileName = marker[1].trim();
    cleaned = cleaned.slice(marker[0].length);
  }
  return { text: cleaned.trim(), fileName };
}

/** Title projection of the marker strip: the member's sentence on one line,
 * with the file name standing in when no sentence survived (B2 r2). */
export function stripAttachmentMarkers(value: string): string {
  const { text, fileName } = dropLeadingAttachmentMarkers(value);
  const cleaned = text.replace(/\s+/g, ' ').trim();
  return cleaned || (fileName ? `📎 ${fileName}` : '');
}

/** Decode the escaped body of a possibly-truncated JSON string: a cut
 * trailing escape (`\`, partial `\uXX`) ends the value; `JSON.parse` decodes
 * the complete escapes that remain. */
function decodeJsonStringBody(escaped: string): string {
  let body = escaped;
  const cutEscape = /(?:\\u[0-9a-fA-F]{0,3}|\\)$/;
  if (cutEscape.test(body)) body = body.slice(0, body.lastIndexOf('\\'));
  try {
    return JSON.parse(`"${body}"`) as string;
  } catch {
    return body;
  }
}

/**
 * Sessions titled before B2 stored the runtime's JSON encoding of the input
 * parts (`[{"display_content":…`) — and only its first 40 characters, so the
 * stored title is a truncated JSON prefix. One rule reads it back: the first
 * `display_content`/`text` string is the member's words (an attachment
 * marker line inside it is the chip's job), the first `file_name`/`name`
 * string stands in as `📎 <name>`, and a prefix with nothing recoverable
 * yields '' so the caller shows the plain default. Never raw JSON.
 */
function recoverRuntimeJsonTitle(rawTitle: string): string {
  const trimmed = rawTitle.trim();
  if (!trimmed.startsWith('[{"') && !trimmed.startsWith('{"')) return '';
  const match = trimmed.match(/"(?:display_content|text|file_name|name)":"((?:[^"\\]|\\.)*)/);
  if (!match) return '';
  const value = decodeJsonStringBody(match[1]);
  if (match[0].startsWith('"display_content"') || match[0].startsWith('"text"')) {
    return stripAttachmentMarkers(value);
  }
  const fileName = value.trim();
  return fileName ? `📎 ${fileName}` : '';
}

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
  delegationLabel = 'Delegation',
  assistantLabel = 'My Assistant',
): string {
  const source = recordValue(session);
  let rawTitle = String(source.title || source.name || '').trim() || fallback;
  if (source.session_kind === 'agent_chat' || source.runtime_source === 'delegation_run'
    || source.session_kind === 'delegation_run' || source.runtime_source === 'delegation') {
    const legacyPrefix = rawTitle.startsWith('Delegation: ');
    const names = legacyPrefix ? rawTitle.slice('Delegation: '.length) : rawTitle;
    rawTitle = (legacyPrefix ? `${delegationLabel}${delegationLabel === 'Delegation' ? ': ' : '：'}` : '')
      + names.split(' ↔ ').map((name) => assistantNameForDisplay(name, assistantLabel)).join(' ↔ ');
  }
  if (!hasSessionBranchIdentity(source)) {
    const defaultTitle = rawTitle.match(/^Session (\d{2}-\d{2} \d{2}:\d{2})$/);
    if (defaultTitle && source.session_kind !== 'agent_chat') {
      return fallback ? `${fallback} ${defaultTitle[1]}` : '';
    }
    // A runtime-JSON title recovers its member text or falls to the plain
    // default — the raw JSON encoding is never a title (B2 r3). Current
    // titles carry the composer's leading attachment markers; the title is
    // the member's sentence, with the file name as fallback (B2 r2).
    if (rawTitle.startsWith('[{"') || rawTitle.startsWith('{"')) {
      return recoverRuntimeJsonTitle(rawTitle) || fallback;
    }
    return stripAttachmentMarkers(rawTitle) || rawTitle;
  }

  let title = rawTitle;
  while (LEGACY_BRANCH_TITLE_SUFFIX.test(title)) {
    title = title.replace(LEGACY_BRANCH_TITLE_SUFFIX, '').trim();
  }
  if (title.startsWith('[{"') || title.startsWith('{"')) {
    return recoverRuntimeJsonTitle(title) || fallback;
  }
  return stripAttachmentMarkers(title) || fallback;
}
