# Order a VPS through the platform

!!! info "IncubaCloud SaaS — paid plans"
    On-demand VPS ordering is available on the hosted service, on paid plans
    (Starter and up). Self-hosted deployments and Free-plan accounts
    [connect their own VPS](connect.md) instead.

Order a Hetzner Cloud VPS without leaving IncubaCloud. The platform creates the
server, locks SSH down to the platform, installs Docker and the proxy, and
registers it as a host ready to receive instances.

## Pricing

VPS usage is billed **by the hour with a monthly cap**: in a calendar month you
never pay more than the monthly price the catalog shows when you order, and
short-lived servers only cost the hours they ran. The charge appears as a line on
your subscription invoice. The price is Hetzner's list price excluding VAT, plus
a management margin, the same on every plan: for the server and its IPv4
address, by the hour at Hetzner's hourly rate with the margin, and never more in
a month than the monthly price. VAT is added on our invoice.

An ordered VPS does not use your plan's host allowance, which counts the servers
you connect yourself, so it never adds an extra host
(see [Billing → Extras](../billing/index.md#extras)).

## Walkthrough

### 1. Open your Hosts dashboard

In your control plane, go to `/cloud/hosts` and click **Order VPS**.

### 2. Pick provider, location and size

The form goes in that order. The provider comes first, with Hetzner chosen by
default; change it if another is listed. The locations and sizes then are
those the provider has in stock right now, read from it when the form opens
and each time you change provider, so nothing you pick is already out of stock.
Choose a location in the region you want, close to your users. Each size shows
the exact hourly and monthly price for that location, before VAT, and that is
what you will pay.

If the provider has nothing in stock, the form says so in plain words and
orders nothing; try again later or choose another provider.

Each size also says how many users at once it is **recommended for**, or that
it is for tests only. Type the number of people who will work in Odoo at the
same time in **How many people will use Odoo at once?**, and once a location is
chosen the cheapest size recommended for them is picked for you; a smaller one
says so under the list. The figure follows Odoo's own formula, explained in
[From zero to Odoo](../getting-started/zero-to-odoo.md#2-choose-the-server).

### 3. Choose the hostname

If the platform manages your DNS, the new host gets a name like
`vps1.<your-subdomain>.incubacloud.io` automatically. You can point your own
DNS at it later for custom domains.

### 4. Confirm

If the panel has no backup destination yet, **Set up managed backups next** is
ticked under the size: a production instance cannot be deployed without one.
Once the server is ordered, the next window offers the
[managed backup storage](../backups/index.md#managed-backup-storage) with each
tier's price, and none chosen. Choose one and confirm it to order it, and it
becomes the panel's default destination; **Skip** orders nothing.

Provisioning takes a couple of minutes end to end:

1. Create the server at Hetzner.
2. Generate an SSH keypair and lock SSH to key-only access, firewalled to the
   platform.
3. Install Docker and Traefik (Full Setup) and harden the host.
4. Run the health probe.

When the job ends in green, the host shows up in your list, ready for deploys.

**Monitoring.** The server sends its metrics to the platform on port 9443. If you
restrict its outbound traffic, allow that port.

## Cancellation

Open the host, and delete/release it from the host actions. Billing stops with
the hourly meter; the subscription invoice reflects only the hours used (never
more than the monthly cap).

## Troubleshooting

??? note "Ordering is refused on my plan"
    The Free plan cannot order on-demand VPS. Upgrade to a paid plan or
    [connect your own server](connect.md). On a paid plan, ordering needs a card
    saved for your subscription and no unpaid invoices: add the card under
    `My account → IncubaCloud → Payment method`, or pay the invoice under
    `My account → Invoices`, and order again.

??? note "The order is refused: out of stock"
    The provider ran out of a size in that location after the form read its
    stock. The form reads it again and offers only what is left; pick another
    location or size and order again. A failed provision offers **Retry in
    another location**, listing only locations in stock for that size. Failed
    provisions are not billed.

??? note "Provisioning failed: rate limit"
    Hetzner enforces creation rate limits. Wait 5 minutes and retry.

??? note "I want extra storage or a snapshot service"
    Out of scope today. Use the host's local disk plus [backup backends](../backups/index.md)
    pointed at S3 — same outcome, portable across providers.

## See also

- [Connect your own VPS](connect.md) if you already have infrastructure.
- [Hosts overview](index.md).
