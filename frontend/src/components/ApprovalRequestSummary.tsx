import { useTranslation } from 'react-i18next';

import {
  approvalRequestPresentation,
  type ApprovalRequestLike,
} from '../utils/approvalRequestPresentation';
import './ApprovalRequestSummary.css';

type ApprovalRequestSummaryProps = {
  approval: ApprovalRequestLike;
  showDetails?: boolean;
  showTitle?: boolean;
};

export default function ApprovalRequestSummary({
  approval,
  showDetails = false,
  showTitle = true,
}: ApprovalRequestSummaryProps) {
  const { t } = useTranslation();
  const presentation = approvalRequestPresentation(approval);

  return (
    <div className="approval-request-summary">
      {showTitle ? (
        <div className="approval-request-title">
          {t(
            `approvalRequest.actions.${presentation.actionKey}`,
            presentation.actionFallback,
          )}
        </div>
      ) : null}
      {showDetails && presentation.description ? (
        <div className="approval-request-description">{presentation.description}</div>
      ) : null}
      {showDetails && presentation.fields.length > 0 ? (
        <dl className="approval-request-fields">
          {presentation.fields.map((field) => (
            <div key={field.key} className="approval-request-field">
              <dt>{t(`approvalRequest.fields.${field.key}`, field.key)}</dt>
              <dd>
                {field.code ? <code>{field.value}</code> : field.value}
              </dd>
            </div>
          ))}
        </dl>
      ) : null}
    </div>
  );
}
