// POST /api/admin-list
// Returns ALL submission records (including ones not yet cleared for public
// display) so the admin review page can see what's pending. Token-protected
// with ADMIN_TOKEN, same as admin-update. POST (not GET) so the token travels
// in a header and body, never in a URL.

import { listPublicRecords } from './_store.js';
import { getIssueStatuses } from './_linear.js';

const STATE_MAP = {
  'Submitted': 'submitted', 'Triage': 'submitted', 'Backlog': 'submitted',
  'In Review': 'review', 'Todo': 'review',
  'Accepted for Investigation': 'accepted', 'In Progress': 'accepted',
  'In Validation Review': 'validation', 'In Validation': 'validation',
  'Done': 'confirmed', 'Confirmed': 'confirmed',
  'Canceled': 'rejected', 'Cancelled': 'rejected', 'Rejected': 'rejected', 'Duplicate': 'rejected',
};

export default async function handler(req, res) {
  if (req.method !== 'POST') {
    return res.status(405).json({ ok: false, error: 'Method not allowed' });
  }
  const auth = req.headers.authorization || '';
  const token = auth.replace(/^Bearer\s+/i, '');
  if (!process.env.ADMIN_TOKEN || token !== process.env.ADMIN_TOKEN) {
    return res.status(401).json({ ok: false, error: 'Unauthorized' });
  }

  try {
    let records = await listPublicRecords();

    // Refresh live status from Linear where possible.
    if (process.env.LINEAR_API_KEY) {
      const ids = records.map(r => r.linearId).filter(Boolean);
      if (ids.length) {
        try {
          const statuses = await getIssueStatuses(ids);
          records = records.map(r => {
            const stateName = statuses[r.linearId];
            return (stateName && STATE_MAP[stateName]) ? { ...r, status: STATE_MAP[stateName] } : r;
          });
        } catch (e) { /* non-fatal — show stored status */ }
      }
    }

    // Newest first
    records.sort((a, b) => (a.id < b.id ? 1 : -1));
    return res.status(200).json({ ok: true, records });
  } catch (err) {
    console.error('admin-list error:', err);
    return res.status(500).json({ ok: false, error: 'Could not load submissions.' });
  }
}
