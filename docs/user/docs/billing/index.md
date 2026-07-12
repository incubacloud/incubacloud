# Billing

!!! info "IncubaCloud SaaS"
    Billing applies to the hosted service. Self-hosted deployments of the open
    core module have no plans or invoices.

## Plans

| Plan | Monthly | Annual (per month) | Hosts | Instances | Backup retention |
| --- | --- | --- | --- | --- | --- |
| Free | 0 € | 0 € | 1 | 1 (sleeps when idle) | — |
| Starter | 9 € | 7 € | 3 | 5 | 7 days |
| Professional | 29 € | 23 € | 10 | 20 | 14 days |
| Business | 49 € | 39 € | 25 | 50 | 30 days |

Annual saves 22%. You can switch monthly ⇄ annual at any time;
the new rate kicks in on the next renewal.

## Extras

If you need more capacity without jumping a tier, add extra hosts or instances
from `My account → IncubaCloud → Extras`. Per unit and month:

| Plan | Extra host | Extra instance |
| --- | --- | --- |
| Starter | 3 € | 2 € |
| Professional | 5 € | 3 € |
| Business | 7 € | 4 € |

## VPS billing

If you [order a VPS](../hosts/hire.md) through the platform, it is billed **by the
hour with a monthly cap** — you never pay more than the VPS's monthly price, and
short-lived servers only cost the hours they ran. Your plan gives a discount on
the catalog price: 5% (Starter), 10% (Professional), 15% (Business). The line
appears on your subscription invoice. Ordering a VPS beyond your plan's host
quota automatically adds an extra-host line.

If you [bring your own VPS](../hosts/connect.md), nothing extra is billed —
only your IncubaCloud plan.

## Payment

We use [Stripe](https://stripe.com). Payment is by card; the card is saved
(tokenized by Stripe) to charge the recurring subscription.

## Invoices

Invoices are generated automatically and stored under
`My account → Invoices`. They're VAT-compliant for EU customers.
Add your VAT number under `My account → Billing details` to have it appear
on the invoice (and to apply reverse-charge if applicable).

## Changing plans

`My account → IncubaCloud → Change plan`, or from your control plane.

- **Upgrading** — takes effect immediately: limits are raised on the spot and the
  subscription lines switch to the new plan. One upgrade per billing cycle.
  Upgrading from Free goes through checkout first.
- **Downgrading** — scheduled for the start of your next billing cycle; you keep
  the higher tier until then. You can cancel a pending downgrade at any time.

!!! warning "Downgrading with too many resources"
    A downgrade is refused while you use more hosts or instances than the target
    plan allows (unless the excess can be converted to paid extras), or while
    paid-only resources — an on-demand VPS or Managed Backup Storage — are still
    active. Release those first.

## If a payment fails

We retry and notify you. If the subscription stays unpaid, the platform
progressively steps in: external resources are released after ~7 days, your
instances are suspended after ~14 days, and data is deleted after ~30 days.
Paying at any point before deletion restores service immediately.

## Cancellation

If you don't want to continue, downgrade to Free (keeps one instance, no
automatic backups) or request cancellation from the
[contact form](https://www.incubacloud.io/contactus). If you also want your data
deleted, say so in the request.
