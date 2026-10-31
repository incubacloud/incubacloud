# RB-28 — A personal-data breach

**Severity:** critical · **When:** personal data may have been read,
copied, changed, lost or destroyed by someone who should not have:
a leaked secret, an intrusion, a backup or bucket left open, data sent to
the wrong customer, a sub-processor (Hetzner, Cloudflare, Stripe) telling
us of a breach on their side.

The GDPR (articles 33 and 34) sets the clock, and Annex A.3 of the Terms
of Service promises the customers to tell them without undue delay. The
clock starts when we become aware of the breach, not when it happened.

## 1. Contain

Stop it first, before anything is measured:

- A leaked secret: rotate it ([RB-01](RB-01-rotate-secret-key.md) for
  `INCUBACLOUD_SECRET_KEY`, [RB-22](RB-22-rotate-smtp-relay-credential.md)
  for the SMTP relay, [RB-10](RB-10-github-app-rate-limited.md) for the
  GitHub App), and revoke the tokens and sessions that used it.
- An intruder on a host: take it off the network from the provider's
  console, keep the disk; do not delete anything that is evidence.
- A tenant whose panel is the way in: suspend it
  ([RB-27](RB-27-illegal-content-notice.md), step 4).
- An open backup or bucket: close its access, keep its access logs.

## 2. Open the record

Write the entry in the breach register (`compliance/registro-brechas.md`,
in the private root repository) now, with what is known, and keep
adding to it. Every breach is recorded, also the ones that are not
notified, with the reason. Never put personal data of the people
affected in it: categories and counts only.

## 3. Find out what it was

- What data, whose, how many people and records, since when, and
  whether it can be undone (encrypted backups whose key did not leak,
  data restored intact).
- Our role for each part:
  - **controller** for the accounts, billing and the platform itself
    (the master);
  - **processor** for what customers keep inside their panels and
    their backups, and their assistant conversations.
- The evidence: the master's and the instances' logs
  ([RB-17](RB-17-instance-log-archive.md)), the hosts' proxy access logs,
  the host shell sessions and the providers' own logs.

## 4. Tell the customers affected (processor)

Within 24 hours of becoming aware, write to the owner of every tenant
whose panel data is affected, even with the facts still incomplete:
what happened, what data, what we did, what we still do not know, and
who to write to. They have their own 72 hours with their authority, and
need ours to meet them. Send what is learned later in follow-ups.

## 5. Notify the AEPD (controller), within 72 hours

The owner decides, a person and never a tool. Notify unless the breach
is unlikely to result in a risk to people's rights and freedoms (for
example, an encrypted copy lost whose key is safe); the AEPD's
*Asesora Brecha* tool helps decide. If notifying:

- on the AEPD's electronic office (sede.aepd.gob.es), «Notificación de
  brechas de datos personales», with the company's certificate;
- with what article 33.3 asks: the nature of the breach, the categories
  and approximate numbers of people and records, a contact
  (`privacy@incubacloud.io`, the one the Privacy Policy gives), the likely consequences and the measures
  taken or proposed;
- in phases when not everything is known yet: the first notice within
  the 72 hours, the rest as it is found. A notice after 72 hours says
  why it is late.

## 6. Tell the people affected, when the risk is high

When the breach is likely to result in a high risk to them (passwords,
payment data, data that allows identity theft), tell the account holders
directly and without undue delay, in plain language: what happened, the
likely consequences, what we did, what they can do (change a password,
watch for phishing) and who to write to. The AEPD's *Comunica-Brecha
RGPD* tool helps decide. Not needed when the data was unintelligible to
whoever got it, or when what we did makes the high risk unlikely.

## 7. Close

Write the cause and what changes so it does not happen again into the
register, and fix it like any other product failure. Close the entry
once the authority, the customers and the people affected have all
been answered.

## Rollback

Nothing to undo: a notice cannot be withdrawn, only completed or
corrected with a follow-up. A breach that turns out not to be one stays
in the register, closed with the reason.
