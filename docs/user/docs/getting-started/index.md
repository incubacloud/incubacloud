# Get started

!!! tip "Never set up a server or a domain?"
    [From zero to Odoo in production](zero-to-odoo.md) goes step by step, from
    nothing to an Odoo your team signs in to: the server, the domain and its
    DNS, the backups, the first deploy and the mail. It explains each word as it
    comes up.

Three short guides that take you from zero to a running Odoo instance:

1. [Create your account](signup.md) — sign up, pick a plan, log in.
2. [Create your first project](first-project.md) — group instances under a customer
   or product; optionally connect a Git repo.
3. [Deploy your first instance](first-deploy.md) — pick a host, an Odoo version,
   the modules; watch it boot.

Each step is independent — if you already have an account, jump straight to step 2.

## The first steps list

While your panel still lacks something to run Odoo, **Projects**, its home
page, shows **First steps**: get a server, set up backups and deploy Odoo,
then two optional ones, your own domain and outgoing mail. Each step says
whether it is done, started or still to do, has the button that takes you to
the screen where you do it and a link to its guide. The button of the step that
comes next is the green one. A step whose last attempt failed says so, and its
button opens the page where you see what failed.

Only users with the Manager role see it: the first two steps are theirs.

- Once the three required steps are done, the list folds to one line; click it
  to see the steps again.
- **×** in its corner closes the list for you; other users still see it.
- To bring it back, click **First steps** next to the number of projects, or
  in the menu under your name.

When every step is done, the list goes away for good. A panel that already
had its first deployment when the list arrived does not show it for the optional
steps; you can still open it from the menu under your name.

## What you'll need

- An account with one of the identity providers listed on the sign-up page, such
  as GitHub, to sign in with (hosted SaaS).
- A payment method if you choose a paid plan (we use Stripe — card).
- Optionally: a Git repo URL for your custom code.
- Optionally: a VPS you already own (or [order one through the platform](../hosts/hire.md)
  on a paid plan).

## After get-started

Most users move on to:

- [Setting up backups](../backups/index.md) so your data is safe.
- [Connecting your own VPS](../hosts/connect.md) if you bring your own infra.
- [Migrating from Odoo.sh](../migrations/from-odoosh.md) or
  [from another host](../migrations/from-another-host.md) if you're moving in.
