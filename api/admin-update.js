// POST /api/admin-update
// Internal endpoint to (1) clear a submission for public display after PII review,
// (2) attach a rejection reason, or (3) attach an Error Log link when confirmed.
//
// Protected by a shared secret in the ADMIN_TOKEN env var. Send it as:
//   Authorization: Bearer <ADMIN_TOKEN>
//
// This is the human gate: a submission's `request` text is the submitter's own
// words and is NOT shown publicly until reviewedForPII is set true here.

import { updatePublicRecord } from './_store.js';

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
    const body = typeof req.body === 'string' ? JSON.parse(req.body) : (req.body || {});
    const id = (body.id || '').toString();
    if (!id) return res.status(400).json({ ok: false, error: 'Missing id' });

    const patch = {};
    if (typeof body.reviewedForPII === 'boolean') patch.reviewedForPII = body.reviewedForPII;
    if (typeof body.request === 'string')         patch.request = body.request;        // scrubbed text
    if (typeof body.title === 'string')           patch.title = body.title;
    if (typeof body.rejectReason === 'string')    patch.rejectReason = body.rejectReason;
    if (typeof body.errorLog === 'string')        patch.errorLog = body.errorLog;
    if (Array.isArray(body.timeline))             patch.timeline = body.timeline;
    if (typeof body.status === 'string')          patch.status = body.status;

    const updated = await updatePublicRecord(id, patch);
    if (!updated) return res.status(404).json({ ok: false, error: 'Not found' });
    return res.status(200).json({ ok: true, record: updated });
  } catch (err) {
    console.error('admin-update error:', err);
    return res.status(500).json({ ok: false, error: 'Update failed' });
  }
}
