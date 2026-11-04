import { Link } from 'react-router-dom';
import { useAuth } from '../hooks/useAuth';

export default function AuthCallbackPage() {
  const { loading, error, session } = useAuth();
  return (
    <main style={{ padding: 48, color: '#fff' }}>
      {loading || session ? <p role="status">Completing sign in…</p> : (
        <>
          <p role="alert">{error || 'No valid login session was returned. Please sign in again.'}</p>
          <Link to="/auth">Return to sign in</Link>
        </>
      )}
    </main>
  );
}
