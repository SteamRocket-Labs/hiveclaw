/** B2: the approval card names the action in the member's words; tool ids
 * (snake_case) stay behind the card's collapsed details disclosure. */
export type ApprovalActionLabel = {
  /** i18n key for the action phrase. */
  key: string;
  /** English fallback for locales without the key yet. */
  fallback: string;
};

const WORKSPACE_READ_FLAGS: Record<string, RegExp> = {
  ls: /^-[1aAdFhlrt]+$/,
  wc: /^-[clmwL]+$/,
  grep: /^-[EFHhcilnsvwx]+$/,
  cat: /^-[benstuvAET]+$/,
};

/** Presentation only: recognize a small, literal workspace-read syntax.
 * Shell evaluation, other commands, and unknown paths keep the cautious copy;
 * this never decides whether a command is approved or allowed to execute. */
export function isSimpleWorkspaceRead(command: unknown): boolean {
  if (typeof command !== 'string' || !/^[\w./-]+(?:[ \t]+[\w./-]+)*$/.test(command.trim())) return false;
  const [executable, ...args] = command.trim().split(/[ \t]+/);
  const flags = Object.prototype.hasOwnProperty.call(WORKSPACE_READ_FLAGS, executable) ? WORKSPACE_READ_FLAGS[executable] : null;
  if (!flags) return false;
  let operands = 0;
  let optionsEnded = false;
  for (const arg of args) {
    if (!optionsEnded && arg === '--') { optionsEnded = true; continue; }
    if (!optionsEnded && arg.startsWith('-')) {
      if (!flags.test(arg)) return false;
      continue;
    }
    // Hidden paths include .env, shell history, .ssh and credential stores.
    if (arg.startsWith('/') || arg.split('/').some(part => part !== '.' && part.startsWith('.'))) return false;
    if (/(?:^|\/)(?:id_rsa|id_dsa|id_ecdsa|id_ed25519|credentials(?:\.json)?|secrets(?:\.json)?)$|\.(?:pem|key|env(?:\.[\w.-]+)?)$/i.test(arg)) return false;
    operands += 1;
  }
  return operands >= (executable === 'grep' ? 2 : executable === 'ls' ? 0 : 1);
}

// Exact tool names only: an exact name carries a known verb. File verbs reuse
// the existing toolTitle.* keys instead of duplicates (B2 r2).
const EXACT_TOOLS: Record<string, ApprovalActionLabel> = {
  create_employee_from_request: { key: 'sessionWorkbench.approvalAction.createEmployee', fallback: 'Create a digital employee' },
  run_command: { key: 'agent.chat.disclosure.toolTitle.runCommand', fallback: 'Run command' },
  start_hr_agent_handoff: { key: 'sessionWorkbench.approvalAction.createEmployee', fallback: 'Create a digital employee' },
  send_email: { key: 'sessionWorkbench.approvalAction.sendEmail', fallback: 'Send an email' },
  reply_email: { key: 'sessionWorkbench.approvalAction.replyEmail', fallback: 'Reply to an email' },
  send_feishu_message: { key: 'sessionWorkbench.approvalAction.sendMessage', fallback: 'Send a message' },
  send_web_message: { key: 'sessionWorkbench.approvalAction.sendMessage', fallback: 'Send a message' },
  send_channel_message: { key: 'sessionWorkbench.approvalAction.sendMessage', fallback: 'Send a message' },
  send_channel_file: { key: 'sessionWorkbench.approvalAction.sendFile', fallback: 'Send a file' },
  upload_image: { key: 'sessionWorkbench.approvalAction.sendFile', fallback: 'Send a file' },
  delegate_to_agent: { key: 'agent.chat.disclosure.toolTitle.delegationStep', fallback: 'Hand to another digital employee' },
  send_message_to_agent: { key: 'agent.chat.disclosure.toolTitle.delegationStep', fallback: 'Hand to another digital employee' },
  write_file: { key: 'agent.chat.disclosure.toolTitle.writeFile', fallback: 'Write file' },
  fs_write: { key: 'agent.chat.disclosure.toolTitle.writeFile', fallback: 'Write file' },
  edit_file: { key: 'agent.chat.disclosure.toolTitle.editFile', fallback: 'Edit file' },
  delete_file: { key: 'agent.chat.disclosure.toolTitle.deleteFile', fallback: 'Delete file' },
  browser_action: { key: 'sessionWorkbench.approvalAction.browser', fallback: 'Operate a web page' },
  browser_manage: { key: 'sessionWorkbench.approvalAction.browser', fallback: 'Operate a web page' },
  plaza_create_post: { key: 'sessionWorkbench.approvalAction.plazaPost', fallback: 'Publish a post' },
  plaza_add_comment: { key: 'sessionWorkbench.approvalAction.plazaComment', fallback: 'Comment on a post' },
  feishu_doc_create: { key: 'sessionWorkbench.approvalAction.feishuDocCreate', fallback: 'Create a Feishu document' },
  feishu_doc_delete: { key: 'sessionWorkbench.approvalAction.feishuDocDelete', fallback: 'Delete a Feishu document' },
  feishu_doc_share: { key: 'sessionWorkbench.approvalAction.feishuDocShare', fallback: 'Share a Feishu document' },
  feishu_base_record_delete: { key: 'sessionWorkbench.approvalAction.feishuBaseDelete', fallback: 'Delete a Feishu Base record' },
  feishu_calendar_delete: { key: 'sessionWorkbench.approvalAction.feishuCalendarDelete', fallback: 'Delete a Feishu calendar event' },
  feishu_approval_create: { key: 'sessionWorkbench.approvalAction.feishuApprovalCreate', fallback: 'Submit a Feishu approval' },
};

// Unmapped tools in a family never guess a verb: the neutral phrase names the
// surface, and the exact-name table above carries every known verb (B2 r2:
// a delete or share must never read as an edit).
const PREFIX_TOOLS: Array<[prefix: string, ApprovalActionLabel]> = [
  ['feishu_doc', { key: 'sessionWorkbench.approvalAction.feishuDoc', fallback: 'Use Feishu documents' }],
  ['feishu_sheet', { key: 'sessionWorkbench.approvalAction.feishuSheet', fallback: 'Use Feishu spreadsheets' }],
  ['feishu_base', { key: 'sessionWorkbench.approvalAction.feishuBase', fallback: 'Use Feishu Base' }],
  ['feishu_calendar', { key: 'sessionWorkbench.approvalAction.feishuCalendar', fallback: 'Use Feishu calendars' }],
  ['feishu_task', { key: 'sessionWorkbench.approvalAction.feishuTask', fallback: 'Use Feishu tasks' }],
  ['feishu_approval', { key: 'sessionWorkbench.approvalAction.feishuApproval', fallback: 'Handle Feishu approvals' }],
];

/** The known-action presentation for a tool id, or null when the tool name
 * alone says nothing safe (callers fall back to a generic phrase). */
export function approvalActionFor(toolName: string | null | undefined): ApprovalActionLabel | null {
  const name = String(toolName || '').trim();
  return (Object.prototype.hasOwnProperty.call(EXACT_TOOLS, name) ? EXACT_TOOLS[name] : null)
    ?? PREFIX_TOOLS.find(([prefix]) => name.startsWith(prefix))?.[1]
    ?? null;
}

export function approvalActionLabel(
  toolName: string | null | undefined,
  t: (key: string, fallback: string) => string,
): string {
  const known = approvalActionFor(toolName);
  if (known) return t(known.key, known.fallback);
  return t('sessionWorkbench.approvalAction.generic', 'An externally visible action');
}
