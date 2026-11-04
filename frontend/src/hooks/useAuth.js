import { supabase } from '../lib/supabase';
import { createAuthStore } from '../lib/authStore';
import { cacheGitHubSession, callbackError, cleanCallbackUrl } from '../lib/authFlow';

const returnError = callbackError(window.location);
export const useAuth = createAuthStore(supabase.auth, {
  onSession: (session) => cacheGitHubSession(session),
  afterInitialize: () => {
    cleanCallbackUrl();
    if (returnError) useAuth.setState({ error: returnError });
  },
});

if (import.meta.hot) import.meta.hot.dispose(() => useAuth.getState().dispose());
