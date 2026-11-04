import { createClient } from '@supabase/supabase-js';
import { callbackUrl, cacheGitHubSession } from './authFlow';

const supabaseUrl = import.meta.env.VITE_SUPABASE_URL;
const supabaseAnonKey = import.meta.env.VITE_SUPABASE_ANON_KEY;

if (!supabaseUrl || !supabaseAnonKey) {
  throw new Error(
    'Missing Supabase configuration. Set VITE_SUPABASE_URL and VITE_SUPABASE_ANON_KEY.',
  );
}

export const supabase = createClient(supabaseUrl, supabaseAnonKey, {
  auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: true },
});

export async function getAccessToken() {
  const { data, error } = await supabase.auth.getSession();
  if (error) throw error;
  return data.session?.access_token ?? null;
}

export async function login(email, password) {
  const { data, error } = await supabase.auth.signInWithPassword({ email, password });
  if (error) throw error;
  return data;
}

export async function signUp(email, password) {
  const { data, error } = await supabase.auth.signUp({
    email, password, options: { emailRedirectTo: callbackUrl(window.location.origin) },
  });
  if (error) throw error;
  return data;
}

export async function loginWithGitHub() {
  const redirectTo = callbackUrl(window.location.origin);

  const { data, error } = await supabase.auth.signInWithOAuth({
    provider: 'github',
    options: { redirectTo, scopes: 'repo read:user user:email' },
  });
  if (error) throw error;
  return data;
}

export async function getGitHubToken() {
  const { data, error } = await supabase.auth.getSession();
  if (error) throw error;
  return cacheGitHubSession(data.session);
}
