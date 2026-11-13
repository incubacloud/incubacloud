# RB-41 — The panel's outgoing address changed

**Severity:** critical (`panel_address_changed`) · **When:** the alert is
raised, Odoo.sh writes that the address of the project's production
changed, or the jobs of a hardened host stop connecting after the panel
moved.

## Why the panel can lose its hosts

A hardened host lets SSH in from a list when it has one: the addresses in
its *allowed SSH IPs* (*Also allow my IP* in the hardening dialog adds
yours), the panel's address when a module that extends the panel knows
it, and the address the hardening run came from, which the run reads on
the host. A host without a list lets any address in, key only.

So a host with a list admits the panel by the address it had when the
host was last hardened. When the panel leaves with another address, that
host refuses it: jobs fail to connect and `host_unreachable` follows.

## Where the alert comes from

On Odoo.sh, when the address of a production instance changes, the
platform calls `GET /_odoo.sh/ip-change?old=<old>&new=<new>` on the
database. The panel then raises `panel_address_changed`, critical, naming
the hardened hosts whose list does not admit the new address. With no new
address given, or one that is not a public address, it names every
hardened host with a list.

The route answers only where `ODOO_STAGE` is `production` (Odoo.sh's
production); elsewhere it answers 404. Odoo.sh's platform answers 403 to
any caller but itself. The alert never changes a host: the request carries
no credential.

Odoo.sh's documentation does not say whether the request comes before or
after the change; read it as after. The panel may already be out of the
hosts it names.

The same notice again (same old and new address) is ignored, whether the
alert is still open or closed. A notice of another change closes the open
alert and raises it again, so that it is sent again.

## Check the new address first

The request proves nothing about who sent it. Before you let any address
into a host, check it against what Odoo.sh shows the project's
administrators: the email Odoo.sh sends when the address changes, or the
project's page on Odoo.sh. When the alert names no new address, find it
there.

## Let the panel in again

For each host the alert names:

1. Open a shell on the host from your provider's console (or by SSH from
   an address its list admits) and let the new address in, live, once
   checked:

   ```bash
   sudo nft add element inet filter ic_ssh_allow '{ <new address> }'
   ```

   For an IPv6 address the set is `ic_ssh_allow6`, which a host hardened
   before 27-oct-2026 lacks. If `nft` says the set does not exist, the host
   was hardened before the sets: add the address to the SSH rule in
   `/etc/nftables.conf` and load it with `sudo nft -f /etc/nftables.conf`.
2. In the panel, run **Apply Hardening** on the host. The run comes from
   the new address and keeps it in the rules the host loads at boot: the
   change of step 1 alone is lost at the next reboot.

The alert drops a host once it is hardened again after the change; once
the list it last confirmed admits the new address; or once it is
archived, deleted or no longer hardened. It closes when none is left.

The list judged is the one the host confirmed: the one its last hardening
applied, or the one it answered to a live change of the list made by a
module that extends the panel. An edit of the host's *allowed SSH IPs* by
hand reaches the host only with the next hardening run, so it does not
take the host out of the alert, and a host whose list was edited since
its last hardening is named, whatever the field says: what it lets in is
unknown until *Apply Hardening* runs.

## Avoid it

- On Odoo.sh, leave a host's list empty (SSH open to any address, key
  only), or add to it an address that does not move and that you can use
  to reach the host: a bastion, an office address.
- Do not write Odoo.sh's current address in a host's list as its only
  way in: it changes with Odoo.sh's moves.

## Rollback

Nothing to roll back: the alert changes no host. To take the new address
out of a host again, `sudo nft delete element inet filter ic_ssh_allow
'{ <address> }'`, then *Apply Hardening* from an address its list admits.
