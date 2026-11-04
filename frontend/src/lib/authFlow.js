const REDIRECT_KEY = 'mclovin_auth_redirect';
const GITHUB_KEY = 'mclovin_github_session';

export function safeDestination(path, fallback = '/app') {
  if (typeof path !== 'string' || !path.startsWith('/') || path.startsWith('//') ||
      path.includes('\\') || /^\/auth(?:[/?#]|$)/.test(path)) return fallback;
  return path;
}

export function rememberDestination(path, storage = sessionStorage) {
  storage.setItem(REDIRECT_KEY, safeDestination(path));
}

export function consumeDestination(storage = sessionStorage) {
  const path = safeDestination(storage.getItem(REDIRECT_KEY));
  storage.removeItem(REDIRECT_KEY);
  return path;
}

export function callbackUrl(origin) {
  // Keep the existing provider allowlisted /auth URL; both callback routes work.
  return new URL('/auth', origin).href;
}

export function callbackError(location) {
  const hash = new URLSearchParams(location.hash.replace(/^#/, ''));
  const query = new URLSearchParams(location.search);
  return (hash.get('error_description') || query.get('error_description') ||
    hash.get('error') || query.get('error') || '').slice(0, 500);
}

export function cleanCallbackUrl(browser = window) {
  const url = new URL(browser.location.href);
  const hash = new URLSearchParams(url.hash.replace(/^#/, ''));
  if (['access_token', 'refresh_token', 'provider_token', 'error', 'error_description'].some((key) => hash.has(key))) {
    url.hash = '';
  }
  if (['/auth', '/auth/callback', '/'].includes(url.pathname)) {
    for (const key of ['code', 'error', 'error_code', 'error_description']) url.searchParams.delete(key);
  }
  browser.history.replaceState(browser.history.state, '', url.pathname + url.search + url.hash);
}

export function cacheGitHubSession(session, storage = sessionStorage) {
  if (!session) {
    storage.removeItem(GITHUB_KEY);
    return null;
  }
  const token = session.provider_token || session.provider_access_token;
  try {
    const cached = JSON.parse(storage.getItem(GITHUB_KEY) || 'null');
    if (cached?.userId === session.user.id && cached.source === 'manual') return cached.token;
  } catch {
    storage.removeItem(GITHUB_KEY);
  }
  if (token) {
    storage.setItem(GITHUB_KEY, JSON.stringify({ userId: session.user.id, token }));
    return token;
  }
  try {
    const cached = JSON.parse(storage.getItem(GITHUB_KEY) || 'null');
    if (cached?.userId === session.user.id) return cached.token;
  } catch {
    // Treat an invalid or stale cached provider session as absent.
  }
  storage.removeItem(GITHUB_KEY);
  return null;
}

export function saveGitHubToken(token, userId, storage = sessionStorage) {
  token = token.trim();
  if (!userId || !token || /\s/.test(token)) throw new Error('Enter a valid GitHub personal access token.');
  storage.setItem(GITHUB_KEY, JSON.stringify({ userId, token, source: 'manual' }));
}

export function clearGitHubToken(storage = sessionStorage) {
  storage.removeItem(GITHUB_KEY);
}
