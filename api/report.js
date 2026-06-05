module.exports = async function handler(req, res) {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
  if (req.method === 'OPTIONS') return res.status(200).end();
  if (req.method !== 'POST') return res.status(405).json({ error: 'Method not allowed' });

  const { official, role, jurisdiction, what_is_wrong, correct_info, submitter_email } = req.body;
  if (!official || !what_is_wrong) return res.status(400).json({ error: 'Missing required fields' });

  const body = [
    'PolicyLogic Error Report',
    '',
    'Official: ' + official + ', ' + (role||'') + ' of ' + (jurisdiction||''),
    'Submitted by: ' + (submitter_email || 'Anonymous'),
    '',
    'What is incorrect:',
    what_is_wrong,
    '',
    'Correct information / source:',
    (correct_info || 'Not provided'),
  ].join('\n');

  try {
    const r = await fetch('https://api.resend.com/emails', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + process.env.RESEND_API_KEY,
      },
      body: JSON.stringify({
        from: 'PolicyLogic Error Reports <onboarding@resend.dev>',
        to: ['anna.rolsma@proton.me'],
        subject: 'PolicyLogic Error Report: ' + official,
        text: body,
      }),
    });

    const j = await r.json();
    if (!r.ok) throw new Error(j.message || JSON.stringify(j));
    res.status(200).json({ ok: true });

  } catch(e) {
    console.error('Resend error:', e.message);
    res.status(500).json({ error: e.message });
  }
}
