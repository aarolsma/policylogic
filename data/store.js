// Storage for public-safe submission records + the assignment counter.
//
// This uses Vercel KV (Upstash Redis) if configured, and otherwise falls back
// to an in-memory store (NON-persistent — only for local dev / preview).
//
// To use Vercel KV in production:
//   1. In Vercel → Storage → create a KV (Upstash) database, connect it to the project.
//   2. That auto-sets KV_REST_API_URL and KV_REST_API_TOKEN env vars.
//   3. npm i @vercel/kv  (add to package.json)
//
// Keys used:
//   submissions:index      → JSON array of all public record objects
//   submissions:counter     → integer, last assignment number used

let kv = null;
try {
  // Lazy import so the file still loads without the package in dev.
  // eslint-disable-next-line
  kv = require('@vercel/kv').kv;
} catch (_) {
  kv = null;
}

// ── In-memory fallback (does NOT persist across deploys/invocations) ──
const mem = { records: [], counter: 0 };

const INDEX_KEY = 'submissions:index';
const COUNTER_KEY = 'submissions:counter';

export async function nextAssignmentNumber() {
  let n;
  if (kv) {
    n = await kv.incr(COUNTER_KEY);
  } else {
    n = ++mem.counter;
  }
  return '#' + String(n).padStart(3, '0');
}

export async function savePublicRecord(record) {
  if (kv) {
    const existing = (await kv.get(INDEX_KEY)) || [];
    existing.push(record);
    await kv.set(INDEX_KEY, existing);
  } else {
    mem.records.push(record);
  }
  return record;
}

export async function listPublicRecords() {
  if (kv) {
    return (await kv.get(INDEX_KEY)) || [];
  }
  return mem.records;
}

// Update a record by assignment id (used by an admin endpoint, see admin-update.js)
export async function updatePublicRecord(id, patch) {
  if (kv) {
    const existing = (await kv.get(INDEX_KEY)) || [];
    const idx = existing.findIndex(r => r.id === id);
    if (idx === -1) return null;
    existing[idx] = { ...existing[idx], ...patch, updated: new Date().toISOString().slice(0,10) };
    await kv.set(INDEX_KEY, existing);
    return existing[idx];
  } else {
    const idx = mem.records.findIndex(r => r.id === id);
    if (idx === -1) return null;
    mem.records[idx] = { ...mem.records[idx], ...patch, updated: new Date().toISOString().slice(0,10) };
    return mem.records[idx];
  }
}
