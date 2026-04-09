import { Navigate, useParams } from 'react-router-dom';

export default function Chat() {
  const { id } = useParams<{ id: string }>();
  return <Navigate to={id ? `/agents/${id}#chat` : '/plaza'} replace />;
}
