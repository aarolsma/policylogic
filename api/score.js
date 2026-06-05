module.exports = async function handler(req, res) {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
  if (req.method === 'OPTIONS') return res.status(200).end();
  if (req.method !== 'POST') return res.status(405).json({ error: 'Method not allowed' });
  const name = req.body.name;
  const role = req.body.role;
  const jurisdiction = req.body.jurisdiction;
  if (!name || !jurisdiction) return res.status(400).json({ error: 'Missing fields' });
  const RESEARCH_PROMPT = "You are a research assistant for PolicyLogic. Search the web thoroughly for this official. Find campaign promises, policy actions, legislation signed, executive orders, and outcome data. Search at least 6 times with different queries. Write detailed notes with numbers, dates, quotes, and sources.";
  const SCORING_PROMPT = "You are a scoring analyst. Return ONLY valid JSON, no markdown, no preamble. Keys: official(name,role,jurisdiction,party,term_start,term_end,sources_searched), summary(narrative,total_promises,delivery_rate_estimate,ambition_level,preliminary_grade,grade_rationale), promises array(normalized,verbatim,source,domain,domain_label,clarity_score,clarity_label,quantitative_target,target_deadline,requires_legislation,requires_federal,difficulty_multiplier,difficulty_label,initiation_score,initiation_label,initiation_evidence,outcome_score,outcome_label,outcome_evidence,attribution_modifier,attribution_note,policy_score,flags(reversed,redefined,credit_overclaimed,externally_blocked,deadline_shifted,scope_reduced),flag_notes,confidence,confidence_note), data_gaps array, methodology_notes. Scoring: Clarity 0-3, Initiation 0-4, Outcome 0-5, Attribution 0.0-1.0, Difficulty 1.0-2.5. policy_score=(clarity+initiation+outcome*attribution)*difficulty. Max 6 promises.";
  try {
    const r1 = await fetch('https://api.anthropic.com/v1/messages', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'x-api-key': process.env.ANTHROPIC_API_KEY, 'anthropic-version': '2023-06-01' },
      body: JSON.stringify({
        model: 'claude-haiku-4-5-20251001',
        max_tokens: 1500,
        system: RESEARCH_PROMPT,
        tools: [{ type: 'web_search_20250305', name: 'web_search' }],
        messages: [{ role: 'user', content: 'Search for ' + name + ', ' + role + ' of ' + jurisdiction + '. Run 6 searches: campaign promises, policy actions, legislation, outcomes, failures, record.' }]
      })
    });
    const j1 = await r1.json();
    if (j1.error) throw new Error('Research: ' + j1.error.message);
    const notes = (j1.content || []).map(function(b) {
      if (b.type === 'text') return b.text;
      if (b.type === 'web_search_tool_result' && b.content) return b.content.map(function(c) { return c.text || c.title || ''; }).join('\n');
      if (b.type === 'server_tool_use' && b.input) return 'SEARCH: ' + JSON.stringify(b.input);
      return '';
    }).filter(Boolean).join('\n\n').slice(0, 3000);
    if (!notes.trim()) throw new Error('Research returned no content');
    const r2 = await fetch('https://api.anthropic.com/v1/messages', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'x-api-key': process.env.ANTHROPIC_API_KEY, 'anthropic-version': '2023-06-01' },
      body: JSON.stringify({
        model: 'claude-haiku-4-5-20251001',
        max_tokens: 3000,
        system: SCORING_PROMPT,
        messages: [{ role: 'user', content: 'Notes about ' + name + ', ' + role + ' of ' + jurisdiction + ':\n\n' + notes + '\n\nReturn scorecard JSON.' }]
      })
    });
    const j2 = await r2.json();
    if (j2.error) throw new Error('Scoring: ' + j2.error.message);
    const text = (j2.content || []).filter(function(b) { return b.type === 'text'; }).map(function(b) { return b.text; }).join('');
    const clean = text.replace(/\`\`\`json/g, '').replace(/\`\`\`/g, '').trim();
    const start = clean.indexOf('{');
    const end = clean.lastIndexOf('}');
    if (start === -1) throw new Error('No JSON found. Got: ' + clean.slice(0, 200));
    const parsed = JSON.parse(clean.slice(start, end + 1));
    res.status(200).json(parsed);
  } catch(e) {
    res.status(500).json({ error: e.message });
  }
}
