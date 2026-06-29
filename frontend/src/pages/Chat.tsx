import { Navigate, useLocation, useParams } from 'react-router-dom';

export function buildAgentChatRedirect(agentId: string | undefined, search = '') {
  return agentId ? `/agents/${agentId}${search}#chat` : '/plaza';
}

export default function Chat() {
  const { id } = useParams<{ id: string }>();
  const location = useLocation();
  return <Navigate to={buildAgentChatRedirect(id, location.search)} replace />;
}
