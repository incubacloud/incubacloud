# From zero to Odoo in production

This guide is for you if you want your company's Odoo running on the internet,
under your own name, and you have never set up a server, a domain or a mail
account for an application. It goes in order, from nothing to an Odoo your
team signs in to, and explains each word the first time it comes up.

The other guides in **Get started** go faster and assume you know these
things. If you already have a server and a domain, start with
[Create your first project](first-project.md).

## What you will end with

- **An Odoo in production**: the copy of Odoo your company works in.
- **Its address**: a name under the panel's own domain to begin with, and your
  own, such as `erp.example.com`, when you want it.
- **Backups every night**, encrypted, kept off the server.
- **Mail that leaves Odoo**: invoices, quotations, password resets.

Plan for about an hour, most of it waiting: for the server to be created, for
the first deploy, and for a DNS change to be seen everywhere.

## Words you will meet

| Word | What it is |
|---|---|
| **Server** (or *VPS*, *host*) | A computer in a data centre, rented by the month or by the hour, where Odoo runs. In the panel it is a *host*. |
| **Domain** | Your name on the internet, such as `example.com`. You rent it from a *registrar*, usually for a year at a time. |
| **DNS** | The internet's address book: it turns a name such as `erp.example.com` into the address (*IP*) of a server. You change it where your domain is managed. |
| **DNS record** | One line of that address book. An **A** record sends a name to an IP address; a **TXT** record holds text that mail services read. |
| **Backup destination** | Storage outside the server where the nightly backups go. Without one, a production instance cannot be deployed. |
| **Instance** | One running Odoo. **Production** is the one your company uses; a **staging** is a copy for trying things without risk. |
| **SMTP server** | The service that delivers the mail Odoo sends. Your mail provider has one. |

## 1. Get your panel

!!! info "IncubaCloud SaaS"
    [Create your account](signup.md) on incubacloud.io and pick a paid plan:
    ordering a server and the managed backup storage, used below, need one.
    Your panel opens at `https://<your-subdomain>.incubacloud.io`.

On a panel you install yourself, follow
[INSTALL.md](https://github.com/incubacloud/incubacloud/blob/19.0/INSTALL.md)
and open `/cloud`.

## 2. Choose the server

The size of the server depends on how many people will use Odoo. This is the
rule the panel recommends with, for one production and one staging on the same
server:

| RAM | Recommended for up to |
|---|---|
| Under 4 GB | Tests only |
| 4 GB | 5 users |
| 8 GB | 15 users |
| 16 GB | 40 users |
| 32 GB | 90 users |

The rule behind it: 2 GB stay for the system, the proxy and the database, each
further GB serves about 3 users, and a vCPU at most 8. It is a starting point:
heavy reports, large imports or many websites ask for more. You can move an
instance to a bigger server later, with **Move to host** on the instance.

!!! info "IncubaCloud SaaS: order it from the panel"
    Go to **Hosts** and press **Order new VPS**. In **How many people will use
    Odoo?** type how many of you there will be, then choose the **Location**:
    the size recommended for you is picked, and every size says how many users
    it is recommended for and its price. The DNS is done for you: the platform
    creates the server's record under its own domain, and you do not need a
    domain to start. See [Order a VPS](../hosts/hire.md).

With your own server, rent one with Ubuntu 22.04 or newer from any provider,
of the size above, and follow [Connect your own VPS](../hosts/connect.md). It
needs a DNS record before the first deploy: step 6 says how.

## 3. Set up the backups

A production instance is not deployed without a backup destination: the
backups of your company's data have to have somewhere to go before there is
data.

!!! info "IncubaCloud SaaS: managed backup storage"
    When the panel has no backup destination yet, **Order VPS** offers
    **Set up managed backups next**, ticked. Once the server is ordered, the
    next window shows the storage tiers with their prices: choose one and
    confirm it, and it becomes the panel's default destination. Nothing more
    to configure. Skipped, you can order it later under **Backup
    Destinations**. See [Managed Backup Storage](../backups/index.md#managed-backup-storage).

Otherwise, create a bucket at an S3-compatible storage provider and register it
under **Backup Destinations → New Backend**, as
[Backup destinations](../backups/index.md#backup-destinations) explains, and
choose it in **Settings → General → Default Backup Destination**.

Either way, keep a copy of the backup **passphrase** in your password manager:
without it nobody can read the backups ([why](../backups/index.md#encryption)).

## 4. Create the project

A project holds your company's Odoo: its production and its stagings. On
**Projects**, press **New Project**, give it a name, and save. You do not need a
Git repository: without one, Odoo comes with the modules you pick from the
catalog. See [Create your first project](first-project.md).

## 5. Deploy Odoo

1. In the project's side menu, press **+** next to **Production**.
2. Give the instance a name, choose the server of step 2 as its host, and pick
   the modules you want to start with (Sales, Invoicing, Inventory…). You can
   add more later.
3. Press **Create Instance**, and then **Deploy**.

The first deploy takes a few minutes; you can follow it in **Recent activity**.
When it ends in green, the instance's address, a name under the server's
domain, opens Odoo's sign-in page. Details in
[Deploy your first instance](first-deploy.md).

## 6. Your own domain

You can work with the address the panel gave the instance. To use your own,
such as `erp.example.com`:

1. **Find the server's IP address**: on **Hosts**, open the server; it is its
   **IP Address**.
2. **Open your domain's DNS** where it is managed: usually your registrar's
   website, in a section called *DNS*, *DNS zone* or *DNS records*. If your
   domain uses another DNS service, the registrar says which.
3. **Add an A record**:

    | Type | Name | Value |
    |---|---|---|
    | A | `erp` | the server's IP address |

    The **Name** is the part before your domain: `erp` for `erp.example.com`.
    Some providers ask for the whole name instead. Leave the TTL as it is.

4. **Wait until it is seen.** Usually a few minutes, sometimes an hour. A DNS
   lookup website, or `dig +short erp.example.com` in a terminal, shows the
   IP address once it is.
5. **Add the name to the instance**: **Networking → Domains**, **+ Add
   domain**, `erp.example.com`, and **Deploy** again. The certificate for HTTPS
   is requested by itself, which is why the record must be in place first.

!!! note "Your own server"
    A server you connect yourself needs one more record before its first
    deploy: a **wildcard** record, `*` (or `*.odoo` for names under
    `odoo.example.com`), pointing to its IP address. It is the domain you give
    the host in **Wildcard Domain**, and each instance gets a name under it.

More choices, such as certificates you already have, in
[Custom domains and certificates](../instances/index.md#custom-domains-and-certificates).

## 7. Mail that leaves Odoo

Odoo sends invoices, quotations and password resets by mail. In production, it
delivers them through the SMTP server you set on the instance: **Networking**,
fields **SMTP Host**, **SMTP Port**, **SMTP Encryption**, **SMTP User** and
**SMTP Password**. A staging never sends mail to anybody
([why](../instances/index.md#email-on-staging)).

1. **Get the values from your mail provider.** Its help pages give the SMTP
   server, the port (usually 587, with STARTTLS) and how to sign in. Many
   providers ask for an *app password* or a sending account instead of your
   own password.
2. **Fill in the fields** and **Deploy** again.
3. **Add the records your provider gives you** to your domain's DNS, as in
   step 6: usually an **SPF** record (TXT) and a **DKIM** record (TXT or
   CNAME). Without them, much of what Odoo sends ends in spam.
4. **Try it**: in Odoo, send a quotation to an address of yours outside the
   company.

## 8. Before you open it to the team

- [x] The instance shows **Running** and opens at your address with HTTPS.
- [x] The instance's **Backups** tab shows a first backup the morning after
      the deploy; **Create Backup** takes one at once
      ([Set up backups](../backups/index.md)).
- [x] The backup passphrase is in your password manager.
- [x] A test mail arrived, and not in spam.
- [x] Your colleagues are invited: Odoo's own users are created in Odoo,
      under *Settings → Users*.

## Coming from another Odoo?

If your company already works in Odoo somewhere else, do steps 1 to 4, and
then follow [Migrations](../migrations/index.md) instead of step 5: the
project and the production instance are made from what you already have.
