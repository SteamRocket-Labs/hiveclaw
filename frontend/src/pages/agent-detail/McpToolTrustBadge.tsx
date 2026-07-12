import { useTranslation } from 'react-i18next';

interface McpToolTrustBadgeProps {
  trustStatus?: string;
  trustTier?: string;
  runtimeApproved?: boolean;
}

export function McpToolTrustBadge({
  trustStatus,
  trustTier,
  runtimeApproved,
}: McpToolTrustBadgeProps) {
  const { t } = useTranslation();
  const approved = runtimeApproved === true;
  return (
    <div className={`tools-manager-mcp-trust ${approved ? 'approved' : 'blocked'}`}>
      <span>
        {approved
          ? t('agent.extensions.metadataApproved', 'Metadata approved')
          : t('agent.extensions.metadataReviewRequired', 'Metadata review required')}
      </span>
      <code>{trustStatus || 'missing'}</code>
      <code>{trustTier || 'untrusted'}</code>
    </div>
  );
}
