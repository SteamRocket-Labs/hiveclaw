import UserManagement from '../UserManagement';

interface WorkspaceUsersSectionProps {
  selectedTenantId: string;
}

export default function WorkspaceUsersSection({
  selectedTenantId,
}: WorkspaceUsersSectionProps) {
  return <UserManagement key={selectedTenantId} />;
}
