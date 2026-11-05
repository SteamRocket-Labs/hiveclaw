import React from 'react';
import { useTranslation } from 'react-i18next';
import './PastedCredentialNotice.css';
import type { AgentChatMessage } from '../agent-detail/chatRuntime';

export function MemberMessageBody({ message }: { message: AgentChatMessage }) {
  const noticed = message.role === 'user' && message.pastedCredentialNotice;
  return <>
    <div style={{ whiteSpace: 'pre-wrap' }}>{noticed
      ? <PastedCredentialText content={message.content} /> : message.content}</div>
    {noticed && <PastedCredentialNotice />}
  </>;
}

export function PastedCredentialText({ content }: { content: string }) {
  const { t } = useTranslation();
  return <>{content.split('[REDACTED_SECRET]').map((text, index) => (
    <React.Fragment key={index}>
      {index > 0 && <span className="hidden-credential-mark">{t('agent.chat.hiddenCredential', 'Hidden key')}</span>}
      {text}
    </React.Fragment>
  ))}</>;
}

export function PastedCredentialNotice() {
  const { t } = useTranslation();
  return <div className="pasted-credential-notice">{t('agent.chat.pastedCredentialNotice',
    'The key you pasted was hidden. It will not be saved or sent to the model.')}</div>;
}
