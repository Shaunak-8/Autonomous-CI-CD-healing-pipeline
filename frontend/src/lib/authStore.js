import { create } from 'zustand';

export function createAuthStore(auth, { onSession = () => {}, afterInitialize = () => {}, timeoutMs = 15000 } = {}) {
  let subscription;
  let initializing;
  let initialized = false;
  let version = 0;
  return create((set) => {
    const updateSession = (session) => {
      onSession(session);
      set({ session, user: session?.user || null, loading: false, error: null });
    };
    return {
      session: null, user: null, loading: true, error: null,
      setUser: (user) => set({ user }),
      setSession: updateSession,
      setLoading: (loading) => set({ loading }),
      setError: (error) => set({ error }),
      initialize: () => {
        if (initializing) return initializing;
        if (initialized) return Promise.resolve();
        set({ loading: true, error: null });
        // Never await Supabase methods inside this synchronous event callback.
        if (!subscription) {
          subscription = auth.onAuthStateChange((_event, session) => {
            version += 1;
            updateSession(session);
          }).data.subscription;
        }
        const readVersion = version;
        initializing = (async () => {
          let timer;
          try {
            const result = await Promise.race([
              auth.getSession(),
              new Promise((_, reject) => {
                timer = setTimeout(() => reject(new Error('Authentication timed out. Check your connection and try again.')), timeoutMs);
              }),
            ]);
            if (result.error) throw result.error;
            if (version === readVersion) updateSession(result.data.session);
            initialized = true;
          } catch (error) {
            if (version === readVersion) {
              set({ session: null, user: null, loading: false, error: error.message || 'Unable to restore your session.' });
            }
          } finally {
            clearTimeout(timer);
            afterInitialize();
            initializing = null;
          }
        })();
        return initializing;
      },
      logout: async () => {
        const { error } = await auth.signOut();
        if (error) {
          set({ error: error.message });
          throw error;
        }
        updateSession(null);
      },
      dispose: () => {
        subscription?.unsubscribe();
        subscription = null;
        initialized = false;
      },
    };
  });
}
