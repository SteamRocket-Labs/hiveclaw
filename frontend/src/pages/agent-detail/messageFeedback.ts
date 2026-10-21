import type { RecordSessionFeedbackInput } from '../../api/domains/chat';

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export function buildMessageFeedbackInput(
  messageId: string,
  label: RecordSessionFeedbackInput['label'],
  sessionItem?: { id: string; kind: string },
): RecordSessionFeedbackInput {
  if (sessionItem) {
    if (!UUID_PATTERN.test(sessionItem.id)) throw new Error('This response is not ready for feedback.');
    return { label, item_id: sessionItem.id };
  }
  const input: RecordSessionFeedbackInput = { label };
  if (UUID_PATTERN.test(messageId)) input.message_id = messageId;
  return input;
}
