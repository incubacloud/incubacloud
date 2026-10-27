# Billing

!!! info "IncubaCloud SaaS"
    Billing applies to the hosted service. Self-hosted deployments of the open
    core module have no plans or invoices.

## Plans

| Plan | Monthly | Annual (per month) | Hosts | Production instances | Control plane backups |
| --- | --- | --- | --- | --- | --- |
| Free | 0 € | 0 € | 1 | 1 | — |
| Starter | 9 € | 7 € | 3 | 5 | 7 days |
| Professional | 29 € | 23 € | 10 | 20 | 14 days |
| Business | 49 € | 39 € | 25 | 50 | 30 days |

- **Hosts** are the servers you connect yourself. A VPS you
  [order through the platform](../hosts/hire.md) is billed by the hour and does
  not use this allowance.
- **Control plane backups** are the automatic backups we take of your control
  plane. Your instances' backups keep the retention of their backup backend —
  see [Backups](../backups/index.md#how-long-backups-are-kept).
- On Free, the control plane sleeps when idle and wakes on the next request.

!!! tip "Staging instances are not counted"

    Only **production** instances use up the allowance. Stagings — the test
    copies you clone from production, one per pull request or as many as you
    want to keep — run on servers you already pay for, so we do not charge a
    slot for them. Neither does the limit on extra instances below.

Annual billing is paid once a year and costs 20–22% less than twelve monthly
payments. You choose monthly or annual billing at checkout. Changing plans keeps
that billing period: an existing subscription cannot be moved between monthly
and annual billing.

## Extras

If you need more capacity without jumping a tier, add extra hosts or instances
from `My account → IncubaCloud → Extras`. Per unit and month:

| Plan | Extra host | Extra instance |
| --- | --- | --- |
| Starter | 3 € | 1.50 € |
| Professional | 2.50 € | 1 € |
| Business | 2 € | 0.75 € |

Free has no extras.

- **Adding one** charges your saved card right away for the whole current
  billing period — a full year on annual billing — with no proration. From then
  on it renews with your plan.
- **Removing one** takes effect at your next renewal: you keep it until then, and
  it is not billed again. One you are still using cannot be removed; free the
  host or instance first.
- The price follows your plan: after a plan change, your extras renew at the new
  plan's price.

## VPS billing

If you [order a VPS](../hosts/hire.md) through the platform, it is billed **by the
hour with a monthly cap**: in a calendar month you never pay more than the
monthly price the catalog showed you when you ordered it, and short-lived
servers only cost the hours they ran. The usage appears as a line on your
subscription invoice. An ordered VPS does not use your plan's host allowance,
so it never adds an extra-host line.

If you [bring your own VPS](../hosts/connect.md), we bill nothing for it: it
takes one host of your plan's allowance, or an extra host.

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

- **Upgrading** — takes effect immediately: limits are raised on the spot, and
  your control plane may be briefly unavailable while the new plan is applied.
  There is no proration: the current billing period stays at the price you
  paid, and the new plan's price is billed from your next renewal. You can
  upgrade once per billing cycle; after an upgrade, the next one is possible
  from your next renewal.
- **From Free** — moving from Free to a paid plan goes through checkout: the new
  plan applies once that payment goes through.
- **Downgrading** — scheduled for the start of your next billing cycle; you keep
  the higher tier until then. You can cancel a pending downgrade at any time
  before it applies.

!!! warning "Downgrading with too many resources"
    A downgrade is refused while you use more hosts or instances than the target
    plan allows; towards a paid plan you can choose instead to keep the excess
    as paid extras. A move to Free is also refused while you still have an
    on-demand VPS, active Managed Backup Storage or extras: release, cancel or
    remove them first.

## If a payment fails

We retry and notify you. If the subscription stays unpaid, the platform steps in
progressively, counting from the day the payment was found overdue:

- **After ~7 days** — on-demand VPS and Managed Backup Storage are released. A
  VPS is destroyed once a snapshot of its disk is taken; Managed Backup Storage
  keeps its bucket, but your access to it is revoked.
- **After ~14 days** — your control plane is suspended: stopped, with its data
  kept.
- **After ~30 days** — your control plane is deleted, together with those VPS
  snapshots and storage buckets. As on a cancellation, we keep one copy of the
  control plane for 30 days: see [Coming back](#coming-back).

Paying before the deletion brings your control plane back and gives you access
to your Managed Backup Storage again. A released VPS is not rebuilt for you:
once you have paid, open it in your panel and use **Restore server** to bring
its disk back from the snapshot. Servers you connected yourself are not touched
at any step.

## An unused Free control plane

A Free control plane nobody uses for 14 days is suspended; we email you three
days before. You can reactivate it with one click from
`My account → IncubaCloud`. If it stays suspended for 30 days it is deleted,
with a reminder a week before. Free makes no backups of the control plane, so
nothing is kept.

## Cancellation

To stop paying, downgrade to Free — it applies at your next renewal — or ask us
to cancel through the [contact form](https://www.incubacloud.io/contactus).
Cancelling removes your control plane and closes your subscription. On paid
plans we keep one copy of it for 30 days, as described below; if you want it
destroyed sooner, say so in the request.

### Coming back

When the control plane of a paid plan is removed — because you cancelled, or
after non-payment — we keep **one copy of it for 30 days**. If you come back
within that window, signing in and picking a plan offers you two paths:

- **Recover my data** — we redeploy your control plane and restore that copy
  into it. Your subdomain, data and settings come back as they were at the
  moment of removal. Recovering needs a paid plan: Free has no backup service to
  restore the copy with.
- **Start from scratch** — a new empty control plane on the same subdomain. The
  copy is deleted, so this one asks you to type your subdomain to confirm.

After the 30 days the copy is destroyed and cannot be recovered. Plans without
automatic backups (Free) have no copy to keep, so coming back always means
starting fresh.
