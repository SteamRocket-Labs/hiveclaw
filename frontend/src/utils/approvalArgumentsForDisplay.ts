type Translator = (key: string, fallback: string, values?: Record<string, unknown>) => string;

// Only presentation labels: the request and the arguments sent for execution
// keep their exact keys and values. Unknown schema keys are not member copy.
const FIELD_LABELS: Record<string, string> = {
  name: 'name', first_task: 'first_task', command: 'command', cwd: 'cwd',
  to: 'recipient', recipient: 'recipient', subject: 'subject', body: 'body',
  cc: 'cc', attachments: 'attachments', content: 'content', message: 'message',
  path: 'path', filename: 'filename', file_name: 'file_name', code: 'code',
  channel: 'channel', target: 'target', query: 'query', url: 'url', title: 'title',
  agent_name: 'agent', skill: 'name', script: 'path', args: 'arguments',
};

/** Format the complete proposal with localized schema labels. Strings (including
 * code/JSON supplied as a string) stay literal. Serialize entries directly rather
 * than rebuilding an object: aliases such as to/recipient may share a label, but
 * both values must remain visible. No hidden field is dropped. */
export function approvalArgumentsForDisplay(value: unknown, t: Translator, depth = 0): string {
  const indent = '  '.repeat(depth);
  const childIndent = `${indent}  `;
  if (Array.isArray(value)) {
    return value.length ? `[\n${value.map(item => `${childIndent}${approvalArgumentsForDisplay(item, t, depth + 1)}`).join(',\n')}\n${indent}]` : '[]';
  }
  if (value !== null && typeof value === 'object') {
    const entries = Object.entries(value).filter(([, item]) => item !== undefined);
    if (!entries.length) return '{}';
    return `{\n${entries.map(([key, item], index) => {
      const label = Object.prototype.hasOwnProperty.call(FIELD_LABELS, key)
        ? t(`sessionWorkbench.threadItem.field.${FIELD_LABELS[key]}`, key)
        : t('agent.chat.permission.unnamedParameter', 'Parameter {{number}}', { number: index + 1 });
      return `${childIndent}${JSON.stringify(label)}: ${approvalArgumentsForDisplay(item, t, depth + 1)}`;
    }).join(',\n')}\n${indent}}`;
  }
  return JSON.stringify(value) ?? 'null';
}
