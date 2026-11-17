# Access

The server is yours, and the panel is your tool on it. The server's
**Access** tab says how the panel signs in there, who signed in lately, and
what deleting the server from the panel takes off it. Whoever manages
servers on your panel (the Administrator role) sees it.

## How the panel signs in

- **Account**: the user the panel connects as: the account you gave it, or,
  when you gave it `root` and then hardened the server, the operator account
  the hardening made instead, which runs `sudo` without a password.
- **Address**: where it connects, with the SSH port. The hardening moves the
  port, and the panel follows it.
- **Panel's key**: the fingerprint of the key the panel signs in with, the
  same one `ssh-keygen -lf` prints for its line in the account's
  `authorized_keys`. The panel keeps its private half encrypted and never
  hands it out. A server you connected with a password shows none.
- **Server's host key**: the fingerprint of the key the server presents. The
  panel checks it on every connection and refuses a server that presents
  another.

The panel signs in only to run its jobs, the ones you start and the
scheduled ones (backups, health checks, security updates), and the consoles
you open from the panel. Root access stays yours: you can sign in with your
own keys at any time, and what you change outside the panel is yours too.

## Last sign-ins

The last ten sign-ins over SSH of the past seven days, newest first, each
with the user, the address it came from and whose it looks:

| Whose | Meaning |
| --- | --- |
| the panel | The panel's key, from the address the panel connects from |
| the panel's key from another address | The panel's key, from an address the panel does not connect from |
| another key | A key that is not the panel's |
| a password | A sign-in with a password |
| another user | On a server the panel signs in to with a password, another account than the panel's |

Any row but the first kind is highlighted: it was not the panel. It can be
you, or someone you gave access to. The list comes from the server's
[security logs](security.md); with them off, or on a panel that does not
receive them, the tab says so. **Security view** opens the full view in the
**Metrics** tab, with `sudo`, the changes to the files that decide who may
get in, and fail2ban's bans.

## When you delete the server from the panel

Before the panel forgets the server, its deletion job takes off it:

- the metrics and security log agents, and their schedules;
- the proxy (Traefik) and its directory, and the guard on the proxy network;
- the panel's key, from every account whose `authorized_keys` lists it;
- the operator account and its `sudo` rule, when the panel's hardening made
  them and no other key signs in with that account. With another key in it,
  the account stays: the hardening copied root's keys there when it turned
  off root's own SSH login, so it is how those keys reach root.

Your accounts, root's keys, the SSH configuration and the firewall rules the
hardening wrote stay as they are. The job's log says what it removed and
what it left.

If the panel cannot reach the server at that moment, the job fails and says
so. A server where the panel deployed its proxy then stays in the panel, so
that you can try again once it answers; one where it never did is removed
from the panel all the same.
