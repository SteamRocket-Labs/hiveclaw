import type { RecordSessionFeedbackInput } from '../../api/domains/chat';

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export function buildMessageFeedbackInput(
  messageId: string,
  label: RecordSessionFeedbackInput['label'],
): RecordSessionFeedbackInput {
  const input: RecordSessionFeedbackInput = { label };
  if (UUID_PATTERN.test(messageId)) input.message_id = messageId;
  return input;
}
