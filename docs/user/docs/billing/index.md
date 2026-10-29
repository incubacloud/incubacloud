# Billing

!!! info "IncubaCloud SaaS"
    Billing applies to the hosted service. Self-hosted deployments of the open
    core module have no plans or invoices.

## Plans

| Plan | Monthly | Annual (per month) | Hosts | Production instances | Control plane backups |
| --- | --- | --- | --- | --- | --- |
| Free | 0 € | 0 € | 1 | 1 | — |
| Starter | 19 € | 15.83 € | 3 | 5 | 7 days |
| Professional | 39 € | 32.50 € | 10 | 20 | 30 days |
| Business | 49 € | 40.83 € | 25 | 50 | 30 days |
| Agency | 149 € | 124.17 € | 100 | 150 | 30 days |

Prices exclude VAT.

- **Hosts** are the servers you connect yourself. A VPS you
  [order through the platform](../hosts/hire.md) is billed by the hour and does
  not use this allowance.
- **Control plane backups** are the automatic backups we take of your control
  plane. Your instances' backups keep the retention of their backup backend —
  see [Backups](../backups/index.md#how-long-backups-are-kept).
- On Free, the control plane sleeps when idle and wakes on the next request.
- **Agency** includes its own server for your control plane: a server that runs
  only your control plane, ready within 1 business day of subscribing to Agency
  or changing to it. It is kept for as long as your subscription stays on
  Agency.

!!! tip "Staging instances are not counted"

    Only **production** instances use up the allowance. Stagings — the test
    copies you clone from production, one per pull request or as many as you
    want to keep — run on servers you already pay for, so we do not charge a
    slot for them. Neither does the limit on extra instances below.

Annual billing is paid once a year and costs ten monthly payments: two months
free. You choose monthly or annual billing at checkout. Changing plans keeps
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
| Agency | 1.50 € | 0.50 € |

Prices exclude VAT.

Free has no extras.

- **Adding one** charges your saved card right away for the days left in your
  current billing period: the unit price for your billing period × days left ÷
  days in the period. For example, an extra host on a monthly Starter
  subscription with 12 of its 31 days left costs 3 € × 12 ÷ 31 = 1.16 €, plus
  VAT. From then on it renews with your plan.
- **Removing one** takes effect at your next renewal: you keep it until then, and
  it is not billed again. Nothing is refunded. One you are still using cannot be
  removed; free the host or instance first.
- The price of extras follows your plan: after a plan change, your extras renew
  at the new plan's price.

## VPS billing

If you [order a VPS](../hosts/hire.md) through the platform, it is billed **by the
hour with a monthly cap**: in a calendar month you never pay more than the
monthly price the catalog showed you when you ordered it, and short-lived
servers only cost the hours they ran. The price is Hetzner's list price
excluding VAT, plus a management margin, the same on every plan: for the server
and its IPv4 address, by the hour at Hetzner's hourly rate with the margin, and
never more in a month than the monthly price. Your control plane shows the exact
hourly and monthly price before you order. VAT is added on our invoice. The
usage appears as a line on your subscription invoice. An ordered VPS does not use your plan's
host allowance, so it never adds an extra-host line.

If you [bring your own VPS](../hosts/connect.md), we bill nothing for it: it
takes one host of your plan's allowance, or an extra host.

## Payment

We use [Stripe](https://stripe.com). Payment is by card; the card is saved
(tokenized by Stripe) to charge the recurring subscription.

## Invoices

Invoices are generated automatically and stored under
`My account → Invoices`. They're VAT-compliant for EU customers. What is
charged at once — an upgrade, an extra, or Managed Backup Storage ordered,
reactivated or moved to a bigger tier — gets its own invoice there, with the
same taxes as your subscription invoices.

Prices are shown excluding VAT. Customers in Spain, and customers elsewhere in
the EU without an EU VAT number validated in VIES, are charged Spanish VAT
(21%); businesses elsewhere in the EU with a VIES-validated VAT number are
invoiced under the reverse-charge mechanism; customers outside the EU are not
charged VAT. Enter your VAT number in the billing address at checkout; we check
it in VIES, and while the check is pending you are charged VAT. After your
first order it can only be changed by us: contact us, and the change applies
from your next renewal.

## Support

Support covers the management platform only — not Odoo functional consulting
or custom-code support. Email support is at
[support@incubacloud.io](mailto:support@incubacloud.io), and the times are to
our first reply:

| Plan | Support |
| --- | --- |
| Free | Documentation |
| Starter | Email, first reply within 3 business days |
| Professional | Priority email, first reply within 1 business day |
| Business | Email, first reply the same business day, plus live chat |
| Agency | Email, first reply the same business day, plus live chat |

- **Live chat** (Business and Agency) opens from your customer portal,
  `My account`, during business hours.
- **Business days** are Monday to Friday, except public holidays in Barcelona
  (Spain). **Business hours** are 9:00 to 18:00, Madrid time.

## Changing plans

`My account → IncubaCloud → Change plan`, or from your control plane.

- **Upgrading** — takes effect immediately: limits are raised on the spot, and
  your control plane may be briefly unavailable while the new plan is applied.
  Before applying it we show you what it costs, and your saved card is charged
  the price difference between the two plans for the days left in your current
  billing period: (new price − current price) × days left ÷ days in the period,
  with the same taxes as your subscription invoices. For example, moving from
  Starter to Professional on a monthly subscription with 12 of its 31 days left
  costs (39 € − 19 €) × 12 ÷ 31 = 7.74 €. An amount under 0.50 € is not charged
  at once: it is added to your next renewal invoice. From your next renewal the
  subscription bills the new plan's price. If the payment fails, your plan does
  not change and you are told so; update your card under
  `My account → IncubaCloud → Payment method` and try again. The price of
  extras follows your plan: after a plan change, your extras renew at the new
  plan's price.
- **From Free** — moving from Free to a paid plan goes through checkout: the new
  plan applies once that payment goes through.
- **Downgrading** — scheduled for the start of your next billing cycle; you keep
  the higher tier until then. You can cancel a pending downgrade at any time
  before it applies. Nothing is refunded when you downgrade, remove or cancel
  something: you keep what you paid for until your next renewal, and the change
  applies then.

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

Removing the control plane also releases every VPS you
[ordered through the platform](../hosts/hire.md), as after non-payment: a
snapshot of its disk is taken, the server is destroyed and is no longer billed,
and we email you which ones were released. The disk snapshot, the server's IPv4
address (when it could be held) and its firewall are kept for the same 30 days
as the copy of your control plane, then deleted. Servers you connected yourself
are not touched.

### Coming back

When the control plane of a paid plan is removed — because you cancelled, or
after non-payment — we keep **one copy of it for 30 days**. If you come back
within that window, signing in and picking a plan offers you two paths:

- **Recover my data** — we redeploy your control plane and restore that copy
  into it. Your subdomain, data and settings come back as they were at the
  moment of removal. Recovering needs a paid plan: Free has no backup service to
  restore the copy with. A VPS released with the removal is not rebuilt for you:
  open it in your panel and use **Restore server** to bring it back from its
  disk snapshot, with the same IP address when it could be held.
- **Start from scratch** — a new empty control plane on the same subdomain. The
  copy is deleted, so this one asks you to type your subdomain to confirm.

After the 30 days the copy is destroyed and cannot be recovered. Plans without
automatic backups (Free) have no copy to keep, so coming back always means
starting fresh.
