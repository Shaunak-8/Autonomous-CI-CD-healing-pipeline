import test from 'node:test';
import assert from 'node:assert/strict';
import { requestSession } from '../src/lib/requestAuth.js';

test('ready session sends requests even when Supabase session lock is stalled', async () => {
  const session = { access_token: 'example', expires_at: Date.now() / 1000 + 3600 };
  const auth = { getSession() { throw new Error('must not acquire session lock'); } };
  assert.equal(await requestSession(auth, session), session);
});

test('expired session refresh is bounded before the HTTP request starts', async () => {
  await assert.rejects(requestSession({ getSession: () => new Promise(() => {}) }, null, 5), /Session refresh timed out/);
});

test('expired credentials are replaced with refreshed credentials', async () => {
  const session = { access_token: 'refreshed' };
  assert.equal(await requestSession({ getSession: async () => ({ data: { session } }) }, { expires_at: 0 }), session);
});
