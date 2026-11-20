// Axios starts its timeout after interceptors; bound session refresh separately.
export async function requestSession(auth, session, timeoutMs = 8000) {
  if (session?.access_token && session.expires_at > Date.now() / 1000 + 30) return session;
  let timer;
  try {
    const result = await Promise.race([
      auth.getSession(),
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error('Session refresh timed out. Please sign in again.')), timeoutMs);
      }),
    ]);
    if (result.error) throw result.error;
    return result.data.session;
  } finally {
    clearTimeout(timer);
  }
}
