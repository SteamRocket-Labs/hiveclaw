import LocalAgents from '../LocalAgents';

interface LocalAgentLinkCardProps {
  agentId?: string;
  canManage?: boolean;
}

export default function LocalAgentLinkCard(_props: LocalAgentLinkCardProps) {
  return <LocalAgents />;
}
