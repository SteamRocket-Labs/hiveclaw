import { Navigate, useLocation, useParams } from 'react-router-dom';

export function buildAgentChatRedirect(agentId: string | undefined, search = '') {
  if (!agentId) return '/plaza';
  const params = new URLSearchParams(search);
  const sessionId = params.get('session_id');
  if (sessionId) {
    params.delete('session_id');
    params.delete('manage');
    const rest = params.toString();
    return `/agents/${agentId}/sessions/${sessionId}${rest ? `?${rest}` : ''}`;
  }
  return `/agents/${agentId}${search}#chat`;
}

export default function Chat() {
  const { id } = useParams<{ id: string }>();
  const location = useLocation();
  return <Navigate to={buildAgentChatRedirect(id, location.search)} replace />;
}
