/** B2: the approval card names the action in the member's words; tool ids
 * (snake_case) stay behind the card's collapsed details disclosure. */
export type ApprovalActionLabel = {
  /** i18n key under sessionWorkbench.approvalAction */
  key: string;
  /** English fallback for locales without the key yet. */
  fallback: string;
};

const EXACT_TOOLS: Record<string, ApprovalActionLabel> = {
  create_employee_from_request: { key: 'createEmployee', fallback: 'create a digital employee' },
  start_hr_agent_handoff: { key: 'createEmployee', fallback: 'create a digital employee' },
  send_email: { key: 'sendEmail', fallback: 'send an email' },
  reply_email: { key: 'replyEmail', fallback: 'reply to an email' },
  send_feishu_message: { key: 'sendMessage', fallback: 'send a message' },
  send_web_message: { key: 'sendMessage', fallback: 'send a message' },
  send_channel_message: { key: 'sendMessage', fallback: 'send a message' },
  send_channel_file: { key: 'sendFile', fallback: 'send a file' },
  upload_image: { key: 'sendFile', fallback: 'send a file' },
  delegate_to_agent: { key: 'delegate', fallback: 'hand work to another digital employee' },
  send_message_to_agent: { key: 'delegate', fallback: 'hand work to another digital employee' },
  write_file: { key: 'writeFile', fallback: 'write a file' },
  fs_write: { key: 'writeFile', fallback: 'write a file' },
  edit_file: { key: 'editFile', fallback: 'edit a file' },
  delete_file: { key: 'deleteFile', fallback: 'delete a file' },
  browser_action: { key: 'browser', fallback: 'operate a web page' },
  browser_manage: { key: 'browser', fallback: 'operate a web page' },
  plaza_create_post: { key: 'plazaPost', fallback: 'publish a post' },
  plaza_add_comment: { key: 'plazaComment', fallback: 'comment on a post' },
};

const PREFIX_TOOLS: Array<[prefix: string, ApprovalActionLabel]> = [
  ['feishu_doc', { key: 'feishuDoc', fallback: 'edit a Feishu document' }],
  ['feishu_sheet', { key: 'feishuSheet', fallback: 'edit a Feishu spreadsheet' }],
  ['feishu_base', { key: 'feishuBase', fallback: 'edit a Feishu bitable' }],
  ['feishu_calendar', { key: 'feishuCalendar', fallback: 'change a Feishu calendar' }],
  ['feishu_task', { key: 'feishuTask', fallback: 'change a Feishu task' }],
  ['feishu_approval', { key: 'feishuApproval', fallback: 'submit a Feishu approval' }],
];

export function approvalActionLabel(
  toolName: string | null | undefined,
  t: (key: string, fallback: string) => string,
): string {
  const name = String(toolName || '').trim();
  const exact = EXACT_TOOLS[name];
  if (exact) return t(`sessionWorkbench.approvalAction.${exact.key}`, exact.fallback);
  const prefixed = PREFIX_TOOLS.find(([prefix]) => name.startsWith(prefix));
  if (prefixed) return t(`sessionWorkbench.approvalAction.${prefixed[1].key}`, prefixed[1].fallback);
  return t('sessionWorkbench.approvalAction.generic', 'an externally visible action');
}
