# Create your account

This guide walks you through registering, choosing a plan, paying (if applicable),
and reaching your empty dashboard.

!!! info "What this guide covers"
    Account creation, email verification, plan selection, and first login.
    For changing plan or upgrading later, see the [billing reference](../billing/index.md).

## 1. Pick a plan

Open [the pricing page](https://www.incubacloud.io/pricing) and click the button on
the plan that matches your stage. There are four:

| Plan | Best for | What you get |
| --- | --- | --- |
| **Free** | Looking around | 1 host, 1 sleeping instance, no automatic backups |
| **Starter** | Freelancers & solo devs | 3 hosts, 5 instances, 7-day backups |
| **Professional** | Small businesses | 10 hosts, 20 instances, 14-day backups |
| **Business** | Agencies & mid-size companies | 25 hosts, 50 instances, 30-day backups, direct support |

You can change plan at any moment with no downtime. Free has no card requirement.

## 2. Fill in the signup form

Enter your email, choose a password, and confirm. We send you a verification email
immediately — click the link inside to activate the account.

!!! warning "Verification email not arriving?"
    Check spam first. If still missing after 5 minutes, request a new verification
    link from the login page (`Resend verification email`). Mail providers occasionally
    delay first-time messages from new domains.

## 3. Complete payment (paid plans only)

We use [Stripe](https://stripe.com). Card and SEPA Direct Debit are accepted.
The platform creates the subscription and unlocks your dashboard as soon as the
payment confirms — usually within a few seconds.

Invoices are generated automatically and stored under
`My account → Invoices`. They're VAT-compliant for EU customers
(provide your VAT number during signup or anytime after in `My account → Billing details`).

## 4. Open your dashboard

After signup completes, you land on `/cloud/ui` — your dashboard. It's empty.
That's expected. The header shows your account email on the right.

[:octicons-arrow-right-24: Next: create your first project](first-project.md)

## Verify it worked

- [x] You can log in at `/web/login` with the email you used.
- [x] Your dashboard at `/cloud/ui` loads without errors.
- [x] If you chose a paid plan, the next invoice line item shows the prorated cost.

## Troubleshooting

??? note "Signup says \"email already in use\""
    You may have signed up before. Try the password reset flow at `/web/reset_password`.

??? note "Payment succeeded but dashboard is still locked"
    Reload after 60 seconds. If still locked, [contact us](https://www.incubacloud.io/contactus)
    with the Stripe receipt id (it starts with `pi_` or `ch_`).

??? note "Two-factor authentication"
    We strongly recommend enabling 2FA from `My account → Security` before
    you start adding hosts and customer data.
