# Billing

## Plans

| Plan | Monthly | Annual (per month) | Hosts | Instances | Backup retention |
| --- | --- | --- | --- | --- | --- |
| Free | 0 € | 0 € | 1 | 1 (sleeping) | — |
| Starter | 9 € | 7 € | 3 | 5 | 7 days |
| Professional | 29 € | 23 € | 10 | 20 | 14 days |
| Business | 49 € | 39 € | 25 | 50 | 30 days |

Annual saves 22%. You can switch monthly ⇄ annual at any time;
the new rate kicks in on the next renewal.

## VPS billing

If you [hire a VPS](../hosts/hire.md) through us, the cost is the
**Hetzner catalog price**, prorated to the day. We don't add a fee and we don't
pass on a discount. The line shows up on your next invoice.

If you [bring your own VPS](../hosts/connect.md), nothing extra is billed —
only your IncubaCloud plan.

## Payment

We use [Stripe](https://stripe.com). Card and SEPA Direct Debit are accepted.
For SEPA, the first invoice may take a few business days to confirm.

## Invoices

Invoices are generated automatically and stored under
`My account → Invoices`. They're VAT-compliant for EU customers.
Add your VAT number under `My account → Billing details` to have it appear
on the invoice (and to apply reverse-charge if applicable).

## Changing plans

`My account → Plan → Change plan`. The new plan takes effect immediately.
Differences are prorated:

- **Upgrading** — you're charged the prorated difference today.
- **Downgrading** — you keep the higher tier until the end of the current period,
  then drop to the lower one.

!!! warning "Downgrading with too many resources"
    If you're using more hosts or instances than the lower plan allows,
    the platform refuses the downgrade until you remove the excess.
    Archive what you don't need first.

## Cancellation

`My account → Plan → Cancel subscription`. Your account stays usable until the
end of the current billing period. After that, instances are paused (not deleted)
for 30 days, then archived. You can re-subscribe anytime to restore access.

If you also want your data deleted, request it from the contact form.

!!! note "Reference page is in progress"
    A more detailed page on invoices, taxes, and subscription edge cases is
    coming soon.
