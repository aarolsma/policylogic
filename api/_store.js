// Storage for public-safe submission records + the assignment counter.
// Uses Upstash Redis via @vercel/kv's createClient, reading the connection
// from the "storage_"-prefixed env vars that the connected KV database created.

import { createClient } from '@vercel/kv';

function getKv() {
  const url =
    process.env.storage_KV_REST_API_URL ||
    process.env.STORAGE_KV_REST_API_URL ||
    process.env.KV_REST_API_URL;
  const token =
    process.env.storage_KV_REST_API_TOKEN ||
    process.env.STORAGE_KV_REST_API_TOKEN ||
    process.env.KV_REST_API_TOKEN;
  if (!url || !token) return null;
  return createClient({ url, token });
}

const kv = getKv();

// In-memory fallback (non-persistent — only used if storage env vars are missing)
const mem = { records: [], counter: 0 };

const INDEX_KEY = 'submissions:index';
const COUNTER_KEY = 'submissions:counter';

export async function nextAssignmentNumber() {
  let n;
  if (kv) { n = await kv.incr(COUNTER_KEY); }
  else { n = ++mem.counter; }
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
  if (kv) { return (await kv.get(INDEX_KEY)) || []; }
  return mem.records;
}

export async function updatePublicRecord(id, patch) {
  const stamp = new Date().toISOString().slice(0, 10);
  if (kv) {
    const existing = (await kv.get(INDEX_KEY)) || [];
    const idx = existing.findIndex(r => r.id === id);
    if (idx === -1) return null;
    existing[idx] = { ...existing[idx], ...patch, updated: stamp };
    await kv.set(INDEX_KEY, existing);
    return existing[idx];
  } else {
    const idx = mem.records.findIndex(r => r.id === id);
    if (idx === -1) return null;
    mem.records[idx] = { ...mem.records[idx], ...patch, updated: stamp };
    return mem.records[idx];
  }
}
