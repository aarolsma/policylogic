// PolicyLogic live scoring endpoint — Methodology v2.
//
// ARCHITECTURE (methodology §12.3): the AI assigns BUCKET VALUES only. ALL
// arithmetic — adjusted delivery, difficulty earned, impact, the 60/40 grade
// input, and the letter grade — is computed HERE in deterministic code, ported
// from scoring_engine.py. Prompt drift or model changes cannot move a grade.
// The model surfaces; the code computes.
//
// Every response is flagged as an unreviewed AI draft. PolicyLogic does not treat
// a live-generated grade as final until a human reviews it.

// ============================================================================
// Engine constants — transcribed from scoring_engine.py (the live methodology).
// ============================================================================
const DELIVERY_POINTS = { D4: 12, D3: 9, D2: 6, D1: 3, D0: 0 };
const DELIVERY_ORDER = ['D0', 'D1', 'D2', 'D3', 'D4'];
const DIFFICULTY_MAX = { H3: 5, H2: 3, H1: 1 };
const SCALE_POINTS = { S3: 4, S2: 2, S1: 1 };
const MAGNITUDE_POINTS = { M3: 4, M2: 2, M1: 1 };
const MAGNITUDE_ORDER = ['M1', 'M2', 'M3'];
const THEIR_ROLE_VALUES = [1.0, 0.8, 0.6, 0.4, 0.2, 0.0];
const PROMISE_SCORE_MAX = 25;
const DELIVERY_MAX = 12;
// 6-band scale, lower bound inclusive: A+ 90 / A 85 / B 70 / C 50 / D 30 / F <30
const GRADE_BANDS = [[90, 'A+'], [85, 'A'], [70, 'B'], [50, 'C'], [30, 'D'], [0, 'F']];
const BEHAVIORAL_FLAGS = ['REVERSED', 'REDEFINED', 'EXTERNALLY BLOCKED',
  'CREDIT OVERCLAIMED', 'DEADLINE SHIFTED', 'SCOPE REDUCED'];
const PROMISE_TYPES = ['Quantitative', 'Qualitative', 'Negative'];

const round2 = x => Math.round(x * 100) / 100;
const round4 = x => Math.round(x * 10000) / 10000;

function capDelivery(code, maxCode) {
  return DELIVERY_ORDER.indexOf(code) <= DELIVERY_ORDER.indexOf(maxCode) ? code : maxCode;
}
function floorDelivery(code, minCode) {
  return DELIVERY_ORDER.indexOf(code) >= DELIVERY_ORDER.indexOf(minCode) ? code : minCode;
}
function capMagnitude(code, maxCode) {
  return MAGNITUDE_ORDER.indexOf(code) <= MAGNITUDE_ORDER.indexOf(maxCode) ? code : maxCode;
}

// Time Pressure (scoring_engine.py time_pressure_factor), inclusive lower bound.
function timePressureFactor(tp) {
  if (tp < 0.5) return { mode: 'early', multiplier: tp * 2, provisional: true, reduction: 0 };
  if (tp < 1.0) return { mode: 'on_track', multiplier: 1.0, provisional: false, reduction: 0 };
  let reduction;
  if (tp < 1.25) reduction = 0.10;
  else if (tp < 1.5) reduction = 0.20;
  else reduction = Math.min(0.40, 0.10 * ((tp - 1.0) / 0.25));
  return { mode: 'overdue', multiplier: 1.0 - reduction, provisional: false, reduction };
}

// Score one promise from its buckets. Mirrors score_promise().
function scorePromise(p) {
  const notes = [], reviewFlags = [];
  const f = new Set(p.flags || []);

  if (!(p.clarity >= 2)) {
    return { id: p.id, scored: false,
      reason: 'Clarity < 2 invalid: values statements excluded at qualification.',
      review_flags: ['UNDER REVIEW'] };
  }

  let delivery = p.delivery;

  // Negative promises are binary (D4/D0); intermediate snaps + flags, unless REDEFINED.
  if (p.promise_type === 'Negative' && delivery !== 'D4' && delivery !== 'D0') {
    if (!f.has('REDEFINED')) {
      const snapped = delivery === 'D3' ? 'D4' : 'D0';
      notes.push(`Negative promise intermediate ${delivery} snapped to ${snapped} (binary rule).`);
      reviewFlags.push('CONTESTED');
      delivery = snapped;
    }
  }
  if (f.has('REDEFINED')) { delivery = capDelivery(delivery, 'D2'); notes.push('REDEFINED: capped at D2.'); }
  if (f.has('EXTERNALLY BLOCKED')) {
    if (p.actions_taken) { delivery = floorDelivery(delivery, 'D2'); notes.push('EXTERNALLY BLOCKED: D2 floor (actions taken).'); }
    else notes.push('EXTERNALLY BLOCKED noted; no actions taken, floor not applied.');
  }
  if (f.has('REVERSED')) { delivery = 'D0'; notes.push('REVERSED: forced to D0.'); }
  if (f.has('REVERSED') && f.has('EXTERNALLY BLOCKED')) {
    reviewFlags.push('CONTESTED');
    notes.push('REVERSED and EXTERNALLY BLOCKED both fired — routed to human review.');
  }

  const baseDeliveryPts = DELIVERY_POINTS[delivery];

  let role = p.their_role;
  if (f.has('CREDIT OVERCLAIMED')) { role = Math.min(role, 0.4); notes.push('CREDIT OVERCLAIMED: role capped 0.4.'); }

  const tp = timePressureFactor(p.time_pressure);
  const adjustedDelivery = baseDeliveryPts * tp.multiplier * role;
  const difficultyEarned = DIFFICULTY_MAX[p.difficulty] * (baseDeliveryPts / 12); // raw bucket pts

  let mag = p.magnitude;
  if (p.clarity === 2) { mag = capMagnitude(mag, 'M1'); notes.push('Specificity Cap (Clarity 2): Magnitude M1.'); }
  if (f.has('SCOPE REDUCED')) { mag = capMagnitude(mag, 'M1'); notes.push('SCOPE REDUCED: Magnitude M1.'); }
  const impact = SCALE_POINTS[p.scale] + MAGNITUDE_POINTS[mag];

  const promiseScore = adjustedDelivery + difficultyEarned + impact;
  return {
    id: p.id, scored: true, delivery_code_final: delivery,
    adjusted_delivery: round4(adjustedDelivery), difficulty_earned: round4(difficultyEarned),
    impact, promise_score: round4(promiseScore), provisional: tp.provisional,
    notes, review_flags: reviewFlags,
  };
}

function gradeScorecard(promises) {
  const scored = promises.map(scorePromise);
  const counted = scored.filter(s => s.scored);
  const n = counted.length;
  if (n === 0) return { per_promise: scored, n_scored: 0, grade: null, grade_input_pct: null, card_flags: ['LOW PROMISE COUNT'] };
  const sumAdj = counted.reduce((a, s) => a + s.adjusted_delivery, 0);
  const sumScore = counted.reduce((a, s) => a + s.promise_score, 0);
  const deliveryRatio = sumAdj / (n * DELIVERY_MAX);
  const promiseRatio = sumScore / (n * PROMISE_SCORE_MAX);
  const pct = (deliveryRatio * 0.60 + promiseRatio * 0.40) * 100;
  const letter = GRADE_BANDS.find(([low]) => pct >= low)[1];
  const cardFlags = [];
  if (n < 5) cardFlags.push('LOW PROMISE COUNT');
  if (counted.some(s => s.provisional)) cardFlags.push('PROVISIONAL (early-term)');
  if (counted.some(s => (s.review_flags || []).includes('CONTESTED'))) cardFlags.push('CONTESTED — human review required');
  return { per_promise: scored, n_scored: n, delivery_ratio: round4(deliveryRatio),
    promise_ratio: round4(promiseRatio), grade_input_pct: round2(pct), grade: letter, card_flags: cardFlags };
}

// ============================================================================
// Validation — mirrors adapter.py. Reject malformed/unsupported buckets BEFORE
// scoring; rejected promises do not get scored.
// ============================================================================
function validatePromise(p, i) {
  const id = p.id || `index_${i}`, errors = [];
  if (!PROMISE_TYPES.includes(p.promise_type)) errors.push(`promise_type ${p.promise_type}`);
  if (!(p.delivery in DELIVERY_POINTS)) errors.push(`delivery ${p.delivery}`);
  if (p.promise_type === 'Negative' && p.delivery !== 'D4' && p.delivery !== 'D0')
    errors.push(`negative promise delivery ${p.delivery} must be D4 or D0`);
  if (!(p.difficulty in DIFFICULTY_MAX)) errors.push(`difficulty ${p.difficulty}`);
  if (!(p.scale in SCALE_POINTS)) errors.push(`scale ${p.scale}`);
  if (!(p.magnitude in MAGNITUDE_POINTS)) errors.push(`magnitude ${p.magnitude}`);
  if (!THEIR_ROLE_VALUES.includes(p.their_role)) errors.push(`their_role ${p.their_role}`);
  if (!Number.isInteger(p.clarity) || p.clarity < 2 || p.clarity > 5) errors.push(`clarity ${p.clarity} must be 2..5`);
  if (p.time_pressure != null && (typeof p.time_pressure !== 'number' || p.time_pressure < 0)) errors.push('time_pressure invalid');
  if (typeof p.actions_taken !== 'boolean') errors.push('actions_taken must be boolean');
  const flags = Array.isArray(p.flags) ? p.flags : [];
  const rationales = p.flag_rationales || {};
  for (const fl of flags) {
    if (!BEHAVIORAL_FLAGS.includes(fl)) errors.push(`unknown flag ${fl}`);
    else if (!rationales[fl]) errors.push(`flag ${fl} missing rationale`);
  }
  if (THEIR_ROLE_VALUES.includes(p.their_role) && !p.their_role_anchor) errors.push('their_role missing anchor');
  if (!p.delivery_evidence || !p.delivery_evidence.length) errors.push('no delivery_evidence');
  return { id, errors };
}

// ============================================================================
// Prompts
// ============================================================================
const RESEARCH_PROMPT = "You are a research assistant for PolicyLogic. Search the web thoroughly for this official. Find campaign-era promises (debates, platform, stump speeches, ads — not in-office announcements), and delivery evidence (legislation sponsored, votes, executive actions, outcomes). Search at least 6 times with different queries. Write detailed notes with numbers, dates, quotes, and source URLs. Do not score or conclude — only gather and cite.";

// The scoring model assigns BUCKETS ONLY. It must not compute scores or grades.
const SCORING_PROMPT = `You are a classification assistant for PolicyLogic. Apply Methodology v2 to the research notes. You assign BUCKET VALUES with evidence; you do NOT compute any score, total, or grade — downstream code does all arithmetic. Reason only from the rubric and the notes, never from any prior expectation about a party or official. Identical facts must yield identical buckets.

Return ONLY valid JSON, no markdown, no preamble:
{"official":{"name","role","jurisdiction","party","term_start","term_end","sources_searched"},
"promise_selection":{"identified_estimate","tracked","excluded_nonverifiable","selection_basis"},
"promises":[{"id","promise_text","promise_source","promise_type":"Quantitative|Qualitative|Negative",
"delivery":"D0|D1|D2|D3|D4","delivery_rationale","delivery_evidence":["citation"],
"their_role":1.0,"their_role_anchor","difficulty":"H1|H2|H3","scale":"S1|S2|S3","magnitude":"M1|M2|M3",
"magnitude_rationale","clarity":2,"time_pressure":null,"time_pressure_basis","actions_taken":false,
"actions_evidence":["citation"],"flags":[],"flag_rationales":{},"review_flags":["AI DRAFT"]}],
"data_gaps":[]}

Buckets: Delivery D4=fully delivered/70%+, D3=advanced/40-70%, D2=formal action/10-40%, D1=commitment only/<10%, D0=none. Negative promises BINARY: D4=avoided, D0=occurred (no intermediate). their_role one of 1.0/0.8/0.6/0.4/0.2/0.0 with named anchor. difficulty H3=structural,H2=legislative,H1=executive. scale S3=systemic,S2=regional,S1=narrow. magnitude M3=transformative,M2=significant,M1=minor (by documented population/outcome, NOT salience). clarity 2-5 (no 1; exclude pure values statements). flags only with a flag_rationales entry. When evidence is thin, assign the most defensible bucket and add a review flag — never inflate confidence. Max 6 promises. Always include "AI DRAFT" in review_flags.`;

// ============================================================================
// Handler
// ============================================================================
module.exports = async function handler(req, res) {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
  if (req.method === 'OPTIONS') return res.status(200).end();
  if (req.method !== 'POST') return res.status(405).json({ error: 'Method not allowed' });
  const name = req.body.name, role = req.body.role, jurisdiction = req.body.jurisdiction;
  if (!name || !jurisdiction) return res.status(400).json({ error: 'Missing fields' });

  try {
    // --- Call 1: research ---
    const r1 = await fetch('https://api.anthropic.com/v1/messages', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'x-api-key': process.env.ANTHROPIC_API_KEY, 'anthropic-version': '2023-06-01' },
      body: JSON.stringify({
        model: 'claude-haiku-4-5-20251001', max_tokens: 1500, system: RESEARCH_PROMPT,
        tools: [{ type: 'web_search_20250305', name: 'web_search' }],
        messages: [{ role: 'user', content: 'Search for ' + name + ', ' + role + ' of ' + jurisdiction + '. Run 6 searches: campaign promises, policy actions, legislation, outcomes, failures, record.' }],
      }),
    });
    const j1 = await r1.json();
    if (j1.error) throw new Error('Research: ' + j1.error.message);
    const notes = (j1.content || []).map(function (b) {
      if (b.type === 'text') return b.text;
      if (b.type === 'web_search_tool_result' && b.content) return b.content.map(c => c.text || c.title || '').join('\n');
      if (b.type === 'server_tool_use' && b.input) return 'SEARCH: ' + JSON.stringify(b.input);
      return '';
    }).filter(Boolean).join('\n\n').slice(0, 3000);
    if (!notes.trim()) throw new Error('Research returned no content');

    // --- Call 2: bucket assignment (NO scoring in the model) ---
    const r2 = await fetch('https://api.anthropic.com/v1/messages', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'x-api-key': process.env.ANTHROPIC_API_KEY, 'anthropic-version': '2023-06-01' },
      body: JSON.stringify({
        model: 'claude-haiku-4-5-20251001', max_tokens: 3000, system: SCORING_PROMPT,
        messages: [{ role: 'user', content: 'Notes about ' + name + ', ' + role + ' of ' + jurisdiction + ':\n\n' + notes + '\n\nReturn bucket-assignment JSON.' }],
      }),
    });
    const j2 = await r2.json();
    if (j2.error) throw new Error('Scoring: ' + j2.error.message);
    const text = (j2.content || []).filter(b => b.type === 'text').map(b => b.text).join('');
    const clean = text.replace(/```json/g, '').replace(/```/g, '').trim();
    const start = clean.indexOf('{'), end = clean.lastIndexOf('}');
    if (start === -1) throw new Error('No JSON found. Got: ' + clean.slice(0, 200));
    const parsed = JSON.parse(clean.slice(start, end + 1));

    // --- Validate, then score deterministically HERE ---
    const promises = parsed.promises || [];
    const rejected = [];
    const valid = [];
    promises.forEach((p, i) => {
      const v = validatePromise(p, i);
      if (v.errors.length) rejected.push(v);
      else valid.push({
        id: p.id, promise_type: p.promise_type, delivery: p.delivery,
        their_role: p.their_role, difficulty: p.difficulty, scale: p.scale,
        magnitude: p.magnitude, clarity: p.clarity,
        time_pressure: p.time_pressure == null ? 0.75 : p.time_pressure,
        flags: p.flags || [], actions_taken: !!p.actions_taken,
      });
    });

    const scorecard = gradeScorecard(valid);

    res.status(200).json({
      official: parsed.official || { name, role, jurisdiction },
      promise_selection: parsed.promise_selection,
      scorecard,                       // grade computed in code, not by the model
      promise_buckets: promises,       // the model's assignments + rationales/citations
      rejected,                        // buckets that failed validation, not scored
      data_gaps: parsed.data_gaps || [],
      status: {
        review_state: 'AI DRAFT — UNREVIEWED',
        disclaimer: 'This grade was generated automatically and has not been reviewed by a human. It is a preliminary draft, not a published PolicyLogic scorecard.',
        scoring_model: 'claude-haiku-4-5-20251001',
      },
    });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
};
