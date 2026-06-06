# Submission Tracker + Linear Integration — Setup

This wires the "Report an error" forms to Linear and shows anonymized status on
the public Submission Tracker. You (not the code) need to do a few one-time steps
because they involve credentials and account setup.

## What the code does

```
Scorecard form  →  POST /api/report  →  creates a Linear issue (PRIVATE: holds name/email)
                                      →  stores a public-safe record (NO name/email)

Tracker page    →  GET /api/submissions  →  returns anonymized records,
                                            status synced live from Linear

You, in Linear  →  drag issue through workflow  →  public status updates automatically
PII review      →  POST /api/admin-update  →  clears a record for public display
```

## One-time setup (your steps)

### 1. Linear
- Create a team (or use existing) for corrections.
- Settings → Security & access → **Personal API keys** → create one. Copy it.
- Get the **team ID**: open the team, or run a quick GraphQL query, or copy from
  the URL/settings. (It's a UUID.)
- Set up your workflow states to match the tracker's statuses. Recommended states:
  `Submitted → In Review → Accepted for Investigation → In Validation Review → Confirmed`,
  plus a canceled/rejected state. The names map in `api/submissions.js` (STATE_MAP) —
  edit that map if your state names differ.

### 2. Vercel storage (for the public records + assignment counter)
- Vercel → Storage → create a **KV** (Upstash Redis) database, connect to this project.
  That auto-sets `KV_REST_API_URL` and `KV_REST_API_TOKEN`.
- `npm i @vercel/kv` (already in package.json).

### 3. Environment variables (Vercel → Settings → Environment Variables)
- `LINEAR_API_KEY` = your Linear personal API key
- `LINEAR_TEAM_ID` = your Linear team UUID
- `ADMIN_TOKEN` = any long random string (used to authorize PII-review updates)

### 4. Deploy
- Push to the repo Vercel watches. It auto-deploys the `/api` functions.

## The PII review gate (important)

A submission's free-text `request` is the submitter's own words and may contain
identifying details. The tracker only shows records where `reviewedForPII === true`.
New submissions start `false` and are invisible publicly until you clear them:

```
curl -X POST https://YOURSITE/api/admin-update \
  -H "Authorization: Bearer <ADMIN_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"id":"#009","reviewedForPII":true,"request":"(scrubbed text if edited)"}'
```

You can also use admin-update to set a `rejectReason`, attach an `errorLog` URL when
confirmed, or override `status`/`timeline`.

## Form fields the API expects

The current scorecard forms send: `entry_id`, `title`, `what_is_wrong`,
`correct_info`, `submitter_email`. To capture category and name, add `category`
and `submitter_name` fields to the forms (optional — the API defaults category to
"Other" and name to "(not provided)").

## Files

```
api/report.js        — receives submissions, creates Linear issue, stores public record
api/submissions.js   — returns anonymized list, syncs status from Linear
api/admin-update.js  — PII-review gate + reject reason / error-log link (token-protected)
lib/linear.js        — Linear GraphQL helpers
lib/store.js         — Vercel KV storage (in-memory fallback for dev)
package.json         — @vercel/kv dependency
```

Until the backend is live, the tracker shows built-in sample rows so the page
always renders.
