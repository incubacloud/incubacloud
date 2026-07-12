# Create your account

!!! info "IncubaCloud SaaS"
    This page applies to the hosted service at incubacloud.io. If you self-host
    the open core module, there is no signup — install the module and open `/cloud`.

This guide walks you through registering, choosing a plan, paying (if applicable),
and reaching your own IncubaCloud control plane.

## 1. Pick a plan

Open [the pricing page](https://www.incubacloud.io/pricing) and click the button on
the plan that matches your stage. There are four:

| Plan | Best for | What you get |
| --- | --- | --- |
| **Free** | Looking around | 1 host, 1 instance (sleeps when idle), no automatic backups |
| **Starter** | Freelancers & solo devs | 3 hosts, 5 instances, 7-day backups |
| **Professional** | Small businesses | 10 hosts, 20 instances, 14-day backups |
| **Business** | Agencies & mid-size companies | 25 hosts, 50 instances, 30-day backups, dedicated host |

You can change plan at any moment. Free has no card requirement.

!!! note "Free plan instances sleep"
    On the Free plan your instance is suspended after ~30 minutes without traffic
    and wakes automatically on the next request (the first request after a nap takes
    a few seconds). Paid plans run 24/7.

## 2. Choose your subdomain and sign in

Enter the subdomain you want for your control plane (e.g. `acme` →
`acme.incubacloud.io`), then continue with a social login — **Google** or
**Odoo.com** (GitHub is planned). There is no email-and-password form and no
separate verification email: your identity comes from the provider you pick.

Subdomain rules: 3–63 characters, letters/digits/hyphens (a DNS label). Reserved
names (`www`, `api`, `admin`, …) are not available.

## 3. Complete payment (paid plans only)

We use [Stripe](https://stripe.com); payment is by card, saved for the recurring
subscription. The platform provisions your control plane as soon as the payment
confirms — usually within a few seconds. On the Free plan this step is skipped and
provisioning starts immediately.

Invoices are generated automatically and available under
`My account → Invoices` on incubacloud.io. They're VAT-compliant for EU customers.

## 4. Open your control plane

When provisioning finishes you get your own IncubaCloud at
`https://<your-subdomain>.incubacloud.io`. Log in with the same social account —
tenant instances use IncubaCloud single sign-on.

Your dashboard at `/cloud` is empty. That's expected.

[:octicons-arrow-right-24: Next: create your first project](first-project.md)

## Verify it worked

- [x] `https://<your-subdomain>.incubacloud.io` loads and you can sign in.
- [x] The dashboard at `/cloud` opens without errors.
- [x] `My account` on incubacloud.io shows your plan (and, on paid plans, the subscription).

## Troubleshooting

??? note "The subdomain I want is taken or rejected"
    Labels must be 3–63 chars and unique; a few names are reserved. Pick a variant
    (e.g. `acme-erp`).

??? note "Payment succeeded but my control plane isn't up yet"
    Provisioning normally takes under a minute. If it's still not reachable after a
    few minutes, [contact us](https://www.incubacloud.io/contactus) with the receipt
    id from Stripe.

??? note "Can my teammates log in too?"
    Yes — invite them from your control plane. Additional users join by invitation
    (there is no open signup on your instance).
