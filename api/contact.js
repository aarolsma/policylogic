// POST /api/contact
// Receives a general "get in touch" message and emails it to info@policylogic.io
// via Resend. Dependency-free — uses built-in fetch, no npm packages.
//
// ENV VARS required (Vercel → Project → Settings → Environment Variables):
//   RESEND_API_KEY — Resend API key
//
// NOTE: the "from" address must be on a domain verified in your Resend account.
// If policylogic.io is verified, use noreply@policylogic.io. Otherwise Resend's
// shared onboarding domain (onboarding@resend.dev) works for testing.

const TO_ADDRESS = 'info@policylogic.io';
const FROM_ADDRESS = 'PolicyLogic Contact <noreply@policylogic.io>';

export default async function handler(req, res) {
  if (req.method !== 'POST') {
    return res.status(405).json({ ok: false, error: 'Method not allowed' });
  }

  try {
    const body = typeof req.body === 'string' ? JSON.parse(req.body) : (req.body || {});

    const name    = (body.name || '').toString().slice(0, 200).trim();
    const email   = (body.email || '').toString().slice(0, 200).trim();
    const message = (body.message || '').toString().slice(0, 6000).trim();

    if (!message || message.length < 2) {
      return res.status(400).json({ ok: false, error: 'Please enter a message.' });
    }
    if (!email || !email.includes('@')) {
      return res.status(400).json({ ok: false, error: 'Please enter a valid email address.' });
    }

    const apiKey = process.env.RESEND_API_KEY;
    if (!apiKey) {
      console.error('contact error: RESEND_API_KEY not set');
      return res.status(500).json({ ok: false, error: 'Contact form is not configured yet.' });
    }

    const subject = 'PolicyLogic contact form: ' + (name || email);
    const textBody =
      'New message from the PolicyLogic contact form.\n\n' +
      'Name: ' + (name || '(not provided)') + '\n' +
      'Email: ' + email + '\n\n' +
      'Message:\n' + message + '\n';

    const resp = await fetch('https://api.resend.com/emails', {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer ' + apiKey,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        from: FROM_ADDRESS,
        to: [TO_ADDRESS],
        reply_to: email,
        subject: subject,
        text: textBody,
      }),
    });

    if (!resp.ok) {
      const detail = await resp.text();
      console.error('Resend error ' + resp.status + ': ' + detail);
      return res.status(502).json({ ok: false, error: 'Could not send your message. Please try again later.' });
    }

    return res.status(200).json({ ok: true });
  } catch (err) {
    console.error('contact error:', err);
    return res.status(500).json({ ok: false, error: 'Could not send your message. Please try again later.' });
  }
}
