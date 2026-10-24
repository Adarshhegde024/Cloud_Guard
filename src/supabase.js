import { createClient } from '@supabase/supabase-js'

// Supabase Client for CloudGuard
const SUPABASE_URL = import.meta.env.VITE_SUPABASE_URL || import.meta.env.NEXT_PUBLIC_SUPABASE_URL || 'https://yzzhvzuppqytbbqjpznu.supabase.co'
const SUPABASE_ANON_KEY = import.meta.env.VITE_SUPABASE_ANON_KEY || import.meta.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY || 'sb_publishable_J76UGnfc44HBRozZOdZsHQ_3NnJ2SG4'

export const supabaseConfig = {
  url: SUPABASE_URL,
  key: SUPABASE_ANON_KEY,
  isConfigured: Boolean(SUPABASE_URL && SUPABASE_ANON_KEY),
}

export const supabase = createClient(SUPABASE_URL, SUPABASE_ANON_KEY, {
  auth: {
    persistSession: true,
    autoRefreshToken: true,
    detectSessionInUrl: true,
  },
})

export async function fetchSupabase(table, options = {}) {
  if (!supabaseConfig.isConfigured) return []
  const url = `${SUPABASE_URL.replace(/\/+$/, '')}/rest/v1/${table}`
  const headers = {
    apikey: SUPABASE_ANON_KEY,
    Authorization: `Bearer ${SUPABASE_ANON_KEY}`,
    'Content-Type': 'application/json',
    ...options.headers,
  }
  const response = await fetch(url, { ...options, headers })
  if (!response.ok) {
    const error = await response.json().catch(() => ({}))
    throw new Error(error.message || `Supabase query failed (${response.status})`)
  }
  return response.json()
}
