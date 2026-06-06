// GET /api/submissions
// Returns the PUBLIC, anonymized list of correction submissions for the
// Submission Tracker page. Only records that have passed PII review
// (reviewedForPII === true) are returned. Never exposes submitter name/email.
//
// Optionally syncs current status from Linear so the public statuses reflect
// where each issue actually is in your Linear workflow.

import { listPublicRecords } from './_store.js';
import { getIssueStatuses } from './_linear.js';

// Map your Linear workflow state names → the tracker's status keys.
// Adjust the left-hand strings to match YOUR Linear workflow state names exactly.
const STATE_MAP = {
  'Submitted': 'submitted',
  'Triage': 'submitted',
  'Backlog': 'submitted',
  'In Review': 'review',
  'Todo': 'review',
  'Accepted for Investigation': 'accepted',
  'In Progress': 'accepted',
  'In Validation Review': 'validation',
  'In Validation': 'validation',
  'Done': 'confirmed',
  'Confirmed': 'confirmed',
  'Canceled': 'rejected',
  'Cancelled': 'rejected',
  'Rejected': 'rejected',
  'Duplicate': 'rejected',
};

export default async function handler(req, res) {
  if (req.method !== 'GET') {
    return res.status(405).json({ ok: false, error: 'Method not allowed' });
  }

  try {
    let records = await listPublicRecords();

    // Only publish records that have cleared PII review.
    records = records.filter(r => r.reviewedForPII === true);

    // Optionally refresh live statuses from Linear.
    if (process.env.LINEAR_API_KEY) {
      const ids = records.map(r => r.linearId).filter(Boolean);
      if (ids.length) {
        const statuses = await getIssueStatuses(ids); // { linearId: stateName }
        records = records.map(r => {
          const stateName = statuses[r.linearId];
          if (stateName && STATE_MAP[stateName]) {
            return { ...r, status: STATE_MAP[stateName] };
          }
          return r;
        });
      }
    }

    // Strip to public-safe fields only (defensive — never leak internal fields).
    const safe = records.map(r => ({
      id: r.id,
      category: r.category,
      status: r.status,
      title: r.title,
      scorecard: r.scorecard,
      request: r.request,
      submitted: r.submitted,
      updated: r.updated,
      timeline: r.timeline || [],
      rejectReason: r.status === 'rejected' ? (r.rejectReason || null) : null,
      errorLog: r.status === 'confirmed' ? (r.errorLog || null) : null,
    }));

    // newest first
    safe.sort((a, b) => (a.id < b.id ? 1 : -1));

    res.setHeader('Cache-Control', 's-maxage=60, stale-while-revalidate=300');
    return res.status(200).json({ ok: true, submissions: safe });
  } catch (err) {
    console.error('submissions error:', err);
    return res.status(500).json({ ok: false, error: 'Could not load submissions.' });
  }
}
