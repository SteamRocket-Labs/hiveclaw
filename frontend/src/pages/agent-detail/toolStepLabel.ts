import type { TFunction } from 'i18next';
import { approvalActionFor } from '../session-workbench/approvalActionPresentation';
import { titleForToolMessage } from './chatDisclosureReducer';

const TOOL_TITLE_KEY: Record<string, string> = {
  Thinking: 'agent.chat.phase.thinking',
  Working: 'agent.chat.disclosure.working',
  'Waiting for input': 'agent.chat.disclosure.waiting',
  'Delegation update': 'agent.chat.disclosure.toolTitle.delegationStep',
  'Delegation result': 'agent.chat.disclosure.toolTitle.delegationResult',
  'Loading tools': 'agent.chat.disclosure.toolTitle.loadingTools',
  'Read file': 'agent.chat.disclosure.toolTitle.readFile',
  'Write file': 'agent.chat.disclosure.toolTitle.writeFile',
  'Edit file': 'agent.chat.disclosure.toolTitle.editFile',
  'Delete file': 'agent.chat.disclosure.toolTitle.deleteFile',
  'List files': 'agent.chat.disclosure.toolTitle.listFiles',
  'Search files': 'agent.chat.disclosure.toolTitle.searchFiles',
  'Search web': 'agent.chat.disclosure.toolTitle.searchWeb',
  'Fetch web page': 'agent.chat.disclosure.toolTitle.fetchWebPage',
  'Run command': 'agent.chat.disclosure.toolTitle.runCommand',
  'Edit document': 'agent.chat.disclosure.toolTitle.editDocument',
  'Update tasks': 'agent.chat.disclosure.toolTitle.updateTasks',
  'Workflow step': 'agent.chat.disclosure.toolTitle.workflowStep',
  'Delegation step': 'agent.chat.disclosure.toolTitle.delegationStep',
  'Subagent step': 'agent.chat.disclosure.toolTitle.subagentStep',
  'Schedule step': 'agent.chat.disclosure.toolTitle.scheduleStep',
  'Permission request': 'agent.chat.disclosure.toolTitle.permissionRequest',
  'Plan step': 'agent.chat.disclosure.toolTitle.planStep',
  'Tool call': 'agent.chat.disclosure.toolTitle.toolCall',
  'Runtime event': 'agent.chat.disclosure.toolTitle.runtimeEvent',
  'Goal updated': 'agent.chat.disclosure.toolTitle.goalUpdated',
};

const MEMORY_AND_CREATION: Record<string, [string, string]> = {
  delegate_to_agent: ['delegateWork', 'Delegate work'],
  check_async_task: ['checkColleague', 'Check colleague progress'],
  list_async_tasks: ['checkColleague', 'Check colleague progress'],
  task_output: ['readTaskResult', 'Read task result'],
  read_runtime_result: ['readTaskResult', 'Read task result'],
  send_message_to_agent: ['contactColleague', 'Contact a colleague'],
  send_agent_session_message: ['followUpColleague', 'Send follow-up instructions'],
  read_context_resource: ['readContext', 'Read work context'],
  search_personal_kb: ['searchPersonal', 'Search personal documents'],
  read_personal_kb: ['readPersonal', 'Read personal documents'],
  search_company_kb: ['searchCompany', 'Search company documents'],
  read_company_kb: ['readCompany', 'Read company documents'],
  call_mcp_tool: ['useConnection', 'Use a connected app'],
  save_memory: ['saveMemory', 'Save memory'],
  search_memory: ['searchMemory', 'Search memory'],
  load_memory: ['loadMemory', 'Load memory'],
  update_memory: ['updateMemory', 'Update memory'],
  retire_memory: ['retireMemory', 'Forget memory'],
  create_digital_employee: ['createEmployee', 'Create a digital employee'],
  preview_agent_blueprint: ['previewEmployee', 'Preview a digital employee'],
};

/** Share the conversation's tool words with handover notices. */
export function toolStepLabel(toolName: string | null, t: TFunction, sentinel?: string): string {
  const known = Object.prototype.hasOwnProperty.call(MEMORY_AND_CREATION, toolName ?? '')
    ? MEMORY_AND_CREATION[toolName ?? ''] : undefined;
  if (known) return t(`agent.chat.disclosure.toolTitle.${known[0]}`, known[1]);
  const action = approvalActionFor(toolName);
  if (action) return t(action.key, action.fallback);
  const title = toolName ? titleForToolMessage({ toolName })
    : sentinel && Object.prototype.hasOwnProperty.call(TOOL_TITLE_KEY, sentinel) ? sentinel : titleForToolMessage({ toolName: sentinel });
  return t(TOOL_TITLE_KEY[title] ?? TOOL_TITLE_KEY['Tool call'], title === 'Tool call' ? 'Tool' : title);
}
