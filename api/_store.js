// Storage for public-safe submission records + the assignment counter.
// Talks to Upstash Redis over its HTTP REST API using built-in fetch —
// NO npm dependency, exactly like _linear.js talks to Linear.
//
// Reads connection from the "storage_"-prefixed env vars the KV database created
// (also accepts STORAGE_ / unprefixed as fallbacks).

const REDIS_URL =
  process.env.storage_KV_REST_API_URL ||
  process.env.STORAGE_KV_REST_API_URL ||
  process.env.KV_REST_API_URL;
const REDIS_TOKEN =
  process.env.storage_KV_REST_API_TOKEN ||
  process.env.STORAGE_KV_REST_API_TOKEN ||
  process.env.KV_REST_API_TOKEN;

// In-memory fallback (non-persistent — only used if env vars are missing)
const mem = { records: [], counter: 0 };
const hasRedis = !!(REDIS_URL && REDIS_TOKEN);

const INDEX_KEY = 'submissions:index';
const COUNTER_KEY = 'submissions:counter';

// Run a Redis command via the Upstash REST API. Returns the `result` value.
async function cmd(args) {
  const resp = await fetch(REDIS_URL, {
    method: 'POST',
    headers: {
      'Authorization': 'Bearer ' + REDIS_TOKEN,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(args),
  });
  if (!resp.ok) {
    throw new Error('Upstash REST error ' + resp.status + ': ' + (await resp.text()));
  }
  const json = await resp.json();
  return json.result;
}

async function readIndex() {
  const result = await cmd(['GET', INDEX_KEY]);
  if (!result) return [];
  // Upstash returns the stored string; parse it back to an array.
  return typeof result === 'string' ? JSON.parse(result) : result;
}

async function writeIndex(records) {
  await cmd(['SET', INDEX_KEY, JSON.stringify(records)]);
}

export async function nextAssignmentNumber() {
  let n;
  if (hasRedis) {
    n = await cmd(['INCR', COUNTER_KEY]);
  } else {
    n = ++mem.counter;
  }
  return '#' + String(n).padStart(3, '0');
}

export async function savePublicRecord(record) {
  if (hasRedis) {
    const existing = await readIndex();
    existing.push(record);
    await writeIndex(existing);
  } else {
    mem.records.push(record);
  }
  return record;
}

export async function listPublicRecords() {
  if (hasRedis) return await readIndex();
  return mem.records;
}

export async function updatePublicRecord(id, patch) {
  const stamp = new Date().toISOString().slice(0, 10);
  if (hasRedis) {
    const existing = await readIndex();
    const idx = existing.findIndex(r => r.id === id);
    if (idx === -1) return null;
    existing[idx] = { ...existing[idx], ...patch, updated: stamp };
    await writeIndex(existing);
    return existing[idx];
  } else {
    const idx = mem.records.findIndex(r => r.id === id);
    if (idx === -1) return null;
    mem.records[idx] = { ...mem.records[idx], ...patch, updated: stamp };
    return mem.records[idx];
  }
}
