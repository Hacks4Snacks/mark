"use strict";

// Thin fetch wrapper used by every view. Throws an Error carrying the server's
// `detail` message so callers can `toast(e.message)`.
export async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}
