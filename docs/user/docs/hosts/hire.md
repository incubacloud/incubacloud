# Order a VPS through the platform

!!! info "IncubaCloud SaaS — paid plans"
    On-demand VPS ordering is available on the hosted service, on paid plans
    (Starter and up). Self-hosted deployments and Free-plan accounts
    [connect their own VPS](connect.md) instead.

Order a Hetzner Cloud VPS without leaving IncubaCloud. The platform creates the
server, locks SSH down to the platform, installs Docker and the proxy, and
registers it as a host ready to receive instances.

## Pricing

VPS usage is billed **by the hour with a monthly cap**: you never pay more than
the server's monthly catalog price, and short-lived servers only cost the hours
they ran. Your plan discounts the catalog price — 5% on Starter, 10% on
Professional, 15% on Business. The charge appears as a line on your subscription
invoice.

If the new server exceeds your plan's host quota, an extra-host line is added
automatically (see [Billing → Extras](../billing/index.md#extras)).

## Walkthrough

### 1. Open your Hosts dashboard

In your control plane, go to `/cloud/hosts` and click **Order VPS**.

### 2. Pick size and location

The catalog (sizes, prices, locations, live availability) comes straight from
Hetzner, with your plan discount already applied. Pick the size that fits your
workload and a region close to your users — EU (Helsinki, Nuremberg,
Falkenstein) or US (Ashburn, Hillsboro).

### 3. Choose the hostname

If the platform manages your DNS, the new host gets a name like
`vps1.<your-subdomain>.incubacloud.io` automatically. You can point your own
DNS at it later for custom domains.

### 4. Confirm

Provisioning takes a couple of minutes end to end:

1. Create the server at Hetzner.
2. Generate an SSH keypair and lock SSH to key-only access, firewalled to the
   platform.
3. Install Docker and Traefik (Full Setup) and harden the host.
4. Run the health probe.

When the job ends in green, the host shows up in your list, ready for deploys.

## Cancellation

Open the host, and delete/release it from the host actions. Billing stops with
the hourly meter; the subscription invoice reflects only the hours used (never
more than the monthly cap).

## Troubleshooting

??? note "Ordering is refused on my plan"
    The Free plan cannot order on-demand VPS. Upgrade to a paid plan or
    [connect your own server](connect.md).

??? note "Provisioning failed: out of capacity"
    Hetzner sometimes runs out of stock in a specific region for a specific size.
    Retry with a different region — failed provisions are not billed.

??? note "Provisioning failed: rate limit"
    Hetzner enforces creation rate limits. Wait 5 minutes and retry.

??? note "I want extra storage or a snapshot service"
    Out of scope today. Use the host's local disk plus [backup backends](../backups/index.md)
    pointed at S3 — same outcome, portable across providers.

## See also

- [Connect your own VPS](connect.md) if you already have infrastructure.
- [Hosts overview](index.md).
