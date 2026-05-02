# Hire a Hetzner VPS

Order a Hetzner VPS from inside the platform. We provision it, configure SSH and the
proxy, and hand it back ready as a registered host.

## Pricing

You pay **Hetzner's catalog price** as listed in the form. We don't add a fee,
and we don't pass on a discount. Charges appear on your next invoice prorated to today.

## Walkthrough

### 1. Open `/cloud/ui/hosts`

Click **Hire VPS** (next to **New Host**).

### 2. Pick a region

Closer to your users is faster. EU options (Helsinki, Nuremberg, Falkenstein) are typical.
US options (Ashburn, Hillsboro) work for North-American users.

### 3. Pick a size

We list the available Hetzner plans with vCPU, RAM, disk, and monthly price.
Pick the size that fits your workload.

| Plan | vCPU | RAM | Disk | Best for |
| --- | --- | --- | --- | --- |
| CX22 | 2 | 4 GB | 40 GB | One small Odoo + a staging |
| CX32 | 4 | 8 GB | 80 GB | Production with moderate traffic |
| CX42 | 8 | 16 GB | 160 GB | Multiple production instances |
| CX52 | 16 | 32 GB | 320 GB | Large multi-tenant deployment |

### 4. Confirm and pay

The platform creates the VPS at Hetzner. Provisioning takes 60–90 seconds.

### 5. Watch the log

We:

1. Create the server at Hetzner.
2. Generate an Ed25519 SSH keypair.
3. Configure SSH-only access (password auth disabled).
4. Install Docker and Traefik.
5. Run the health probe.

When the job ends in green, the host shows up in your list.

## Cancellation

Open `/cloud/ui/hosts`, find the host, and click **Cancel VPS**.
We delete the VPS at Hetzner and stop billing immediately. The next invoice
shows a prorated refund line for unused days.

## Troubleshooting

??? note "Provisioning failed: out of capacity"
    Hetzner sometimes runs out of stock in a specific region for a specific size.
    Retry with a different region. Your card is not charged for failed provisions.

??? note "Provisioning failed: rate limit"
    Hetzner enforces creation rate limits. Wait 5 minutes and retry.

??? note "I want to attach extra storage / a backup snapshot service"
    Out of scope today. Use the host's local disk and our [backup backends](../backups/index.md)
    pointed at S3 — that gives you the same outcome with portability across providers.

## See also

- [Connect your own VPS](connect.md) if you already have infrastructure.
- [Hosts overview](index.md).
