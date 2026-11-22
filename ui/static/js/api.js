/**
 * api.js — thin fetch wrapper around the FastAPI service described in
 * INTEGRATION.md. Keeps the X-API-Key header and 401 handling in one place
 * so every other module can just call api("/latest") etc.
 */

const KEY_STORAGE = "fmp_key";

export function getStoredKey() {
  try { return localStorage.getItem(KEY_STORAGE); } catch { return null; }
}

export function setStoredKey(v) {
  try { localStorage.setItem(KEY_STORAGE, v); } catch { /* storage unavailable — key just won't persist */ }
}

export class ApiKeyRequiredError extends Error {}

export async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  const key = getStoredKey();
  if (key) headers["X-API-Key"] = key;

  const r = await fetch(path, { ...opts, headers });
  if (r.status === 401) throw new ApiKeyRequiredError("API key required");
  if (!r.ok) {
    let detail = "";
    try { detail = (await r.json()).detail; } catch { /* body wasn't JSON */ }
    throw new Error(detail || `${r.status} ${r.statusText}`);
  }
  return r.json();
}
