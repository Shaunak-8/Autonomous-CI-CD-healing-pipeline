import test from 'node:test';
import assert from 'node:assert/strict';
import { displayEvent } from '../src/lib/runEvents.js';
import { createAuthStore } from '../src/lib/authStore.js';
import {
  safeDestination, rememberDestination, consumeDestination, callbackUrl,
  cleanCallbackUrl, callbackError, cacheGitHubSession,
  saveGitHubToken, clearGitHubToken,
} from '../src/lib/authFlow.js';

const storage = () => {
  const map = new Map();
  return { getItem: (key) => map.get(key) ?? null,
    setItem: (key, value) => map.set(key, value), removeItem: (key) => map.delete(key) };
};

test('large result and graph messages produce concise dashboard events', () => {
  const result = displayEvent({ type: 'RUN_COMPLETED', message: JSON.stringify({ status: 'failed', total_fixes: 0, total_failures: 465, fixes: Array(1000).fill('large diagnostic payload') }) });
  assert.equal(result.message, 'Run failed: 0 fixes applied; 465 findings remain.');
  assert.ok(result.message.length < 100);
  assert.equal(displayEvent({ type: 'GRAPH_UPDATED', message: '{"nodes":[1,2],"edges":[]}' }).message, 'Dependency graph: 2 files, 0 connections.');
});

function provider(getSession = async () => ({ data: { session: null }, error: null })) {
  let listener;
  let subscriptions = 0;
  return {
    getSession,
    signOut: async () => ({ error: null }),
    onAuthStateChange: (callback) => {
      listener = callback;
      subscriptions += 1;
      return { data: { subscription: { unsubscribe() {} } } };
    },
    emit: (event, session) => listener(event, session),
    subscriptions: () => subscriptions,
  };
}

test('OAuth returns to the existing auth callback without credentials in redirectTo', () => {
  assert.equal(callbackUrl('http://127.0.0.1:5173'), 'http://127.0.0.1:5173/auth');
  assert.equal(callbackUrl('http://localhost:5173'), 'http://localhost:5173/auth');
});

test('return destinations are internal, safe and consumed once', () => {
  for (const path of ['https://example.com', '//example.com', '/\\example.com', '/auth', '/auth/callback']) {
    assert.equal(safeDestination(path), '/app');
  }
  const memory = storage();
  rememberDestination('/app/project/123?tab=logs', memory);
  assert.equal(consumeDestination(memory), '/app/project/123?tab=logs');
  assert.equal(consumeDestination(memory), '/app');
});

test('legacy OAuth fragments and callback codes are removed from browser history', () => {
  for (const path of ['/auth', '/auth/callback', '/']) {
    let cleaned;
    const browser = { location: { href: `http://127.0.0.1:5173${path}?mode=login&code=example-code#access_token=example-access&refresh_token=example-refresh` },
      history: { state: { index: 1 }, replaceState: (_state, _title, url) => { cleaned = url; } } };
    cleanCallbackUrl(browser);
    assert.equal(cleaned, `${path}?mode=login`);
  }
  assert.equal(callbackError({ hash: '#error_description=Access+denied', search: '' }), 'Access denied');
});

test('GitHub provider token survives Supabase refresh and is cleared on logout or account change', () => {
  const memory = storage();
  const session = { user: { id: 'user-1' }, provider_token: 'example-provider-token' };
  assert.equal(cacheGitHubSession(session, memory), 'example-provider-token');
  assert.equal(cacheGitHubSession({ user: session.user }, memory), 'example-provider-token');
  assert.equal(cacheGitHubSession({ user: { id: 'user-2' } }, memory), null);
  cacheGitHubSession(session, memory);
  assert.equal(cacheGitHubSession(null, memory), null);
  assert.equal(cacheGitHubSession({ user: session.user }, memory), null);
});

test('a validated manual GitHub token takes priority until removed or signed out', () => {
  const memory = storage();
  saveGitHubToken('example-manual-token', 'user-1', memory);
  const session = { user: { id: 'user-1' }, provider_token: 'example-oauth-token' };
  assert.equal(cacheGitHubSession(session, memory), 'example-manual-token');
  assert.equal(cacheGitHubSession({ user: session.user }, memory), 'example-manual-token');
  clearGitHubToken(memory);
  assert.equal(cacheGitHubSession(session, memory), 'example-oauth-token');
  cacheGitHubSession(null, memory);
  assert.equal(cacheGitHubSession({ user: session.user }, memory), null);
});

test('empty or malformed manual tokens cannot be saved', () => {
  for (const token of ['', 'token with spaces']) {
    assert.throws(() => saveGitHubToken(token, 'user-1', storage()));
  }
});

test('session restoration subscribes once under duplicate initialization', async () => {
  const session = { user: { id: 'user-1' } };
  const auth = provider(async () => ({ data: { session }, error: null }));
  const store = createAuthStore(auth);
  const first = store.getState().initialize();
  assert.equal(first, store.getState().initialize());
  await first;
  await store.getState().initialize();
  assert.equal(auth.subscriptions(), 1);
  assert.equal(store.getState().session, session);
  assert.equal(store.getState().loading, false);
});

test('OAuth sign-in event wins over a stale initial session read', async () => {
  let resolve;
  const auth = provider(() => new Promise((r) => { resolve = r; }));
  const store = createAuthStore(auth);
  const pending = store.getState().initialize();
  const session = { user: { id: 'github-user' } };
  auth.emit('SIGNED_IN', session);
  resolve({ data: { session: null }, error: null });
  await pending;
  assert.equal(store.getState().session, session);
});

test('failed session restore stops loading and exposes a retryable error', async () => {
  const auth = provider(async () => ({ data: {}, error: new Error('Unable to reach authentication service') }));
  const store = createAuthStore(auth);
  await store.getState().initialize();
  assert.equal(store.getState().loading, false);
  assert.match(store.getState().error, /Unable to reach/);
});

test('session restore timeout cannot leave the app stuck loading', async () => {
  const store = createAuthStore(provider(() => new Promise(() => {})), { timeoutMs: 10 });
  await store.getState().initialize();
  assert.equal(store.getState().loading, false);
  assert.match(store.getState().error, /timed out/);
});

test('logout clears the user and session without duplicate subscriptions', async () => {
  const auth = provider();
  const store = createAuthStore(auth);
  await store.getState().initialize();
  auth.emit('SIGNED_IN', { user: { id: 'user-1' } });
  await store.getState().logout();
  assert.equal(store.getState().session, null);
  assert.equal(store.getState().user, null);
});
