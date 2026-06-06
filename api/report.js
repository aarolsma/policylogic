// POST /api/report
// Receives a correction submission from any scorecard's "Report an error" form,
// creates a Linear issue (private — holds full submitter details), and stores a
// public-safe anonymized record for the Submission Tracker.
//
// ENV VARS required (set in Vercel → Project → Settings → Environment Variables):
//   LINEAR_API_KEY   — Linear personal API key (Linear → Settings → Security & access → API)
//   LINEAR_TEAM_KEY  — the Linear team short key, e.g. "POL"
//   SUBMISSIONS_KV   — (optional) if using Vercel KV / Upstash for public records
//
// This function NEVER returns submitter name/email to the browser.

import { createIssue } from './_linear.js';
import { savePublicRecord, nextAssignmentNumber } from './_store.js';

const CATEGORY_LABELS = {
  misclassified: 'Misclassified',
  evidence: 'Overlooked Evidence',
  score: 'Incorrect Score',
  outdated: 'Outdated Info',
  factual: 'Factual Error',
  other: 'Other',
};

export default async function handler(req, res) {
  if (req.method !== 'POST') {
    return res.status(405).json({ ok: false, error: 'Method not allowed' });
  }

  try {
    const body = typeof req.body === 'string' ? JSON.parse(req.body) : (req.body || {});

    // Fields from the form
    const entryId       = (body.entry_id || '').toString().slice(0, 120);
    const scorecard     = (body.scorecard || body.title || '').toString().slice(0, 200);
    const category      = (body.category || 'other').toString().toLowerCase();
    const whatIsWrong   = (body.what_is_wrong || '').toString().slice(0, 4000);
    const correctInfo   = (body.correct_info || '').toString().slice(0, 4000);
    const submitterName = (body.submitter_name || '').toString().slice(0, 200);   // PRIVATE
    const submitterEmail= (body.submitter_email || '').toString().slice(0, 200);  // PRIVATE

    // Basic validation
    if (!whatIsWrong || whatIsWrong.trim().length < 3) {
      return res.status(400).json({ ok: false, error: 'Please describe what is incorrect.' });
    }
    const categoryLabel = CATEGORY_LABELS[category] || 'Other';

    // Public-safe assignment number (e.g. "#009")
    const assignment = await nextAssignmentNumber();

    // Build the Linear issue body — this is PRIVATE (internal team only).
    // It DOES include submitter name/email so reviewers can follow up.
    const issueTitle = `[${assignment}] ${categoryLabel}: ${scorecard || 'Scorecard correction'}`;
    const issueDescription = [
      `**Assignment:** ${assignment}`,
      `**Category:** ${categoryLabel}`,
      `**Scorecard / Entry:** ${scorecard || '(unspecified)'}`,
      entryId ? `**Entry ID:** ${entryId}` : null,
      '',
      `**What is incorrect:**`,
      whatIsWrong,
      '',
      correctInfo ? `**Correct information / source:**\n${correctInfo}` : null,
      '',
      '---',
      `**Submitter (PRIVATE — do not publish):**`,
      `Name: ${submitterName || '(not provided)'}`,
      `Email: ${submitterEmail || '(not provided)'}`,
    ].filter(Boolean).join('\n');

    // Create the Linear issue
    const issue = await createIssue({
      title: issueTitle,
      description: issueDescription,
    });

    // Store the PUBLIC-SAFE record for the tracker.
    // NOTE: the "request" text is the submitter's words; it is shown publicly,
    // so it must be reviewed for identifying details before status moves past
    // "Submitted". Name and email are NEVER stored in the public record.
    const publicRecord = {
      id: assignment,
      linearId: issue.id,
      linearIdentifier: issue.identifier, // e.g. "PL-42"
      category: categoryLabel,
      status: 'submitted',
      title: deriveTitle(scorecard, categoryLabel, whatIsWrong),
      scorecard: scorecard || 'General',
      request: whatIsWrong + (correctInfo ? `\n\nProposed correction: ${correctInfo}` : ''),
      submitted: today(),
      updated: today(),
      timeline: [[today(), 'Submitted and logged. Awaiting review.']],
      reviewedForPII: false, // gate: must be set true by a human before public display
      errorLog: null,
    };
    await savePublicRecord(publicRecord);

    return res.status(200).json({ ok: true, assignment });
  } catch (err) {
    console.error('report error:', err);
    return res.status(500).json({ ok: false, error: 'Could not submit report. Please try again later.' });
  }
}

function today() {
  return new Date().toISOString().slice(0, 10);
}

function deriveTitle(scorecard, category, whatIsWrong) {
  // Short, neutral summary for the public table.
  if (scorecard && scorecard !== 'General') {
    return `${category} reported on ${scorecard}`;
  }
  return whatIsWrong.trim().slice(0, 80) + (whatIsWrong.length > 80 ? '…' : '');
}
