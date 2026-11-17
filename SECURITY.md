# Security Policy

## Supported Versions

Only the latest minor release of each supported Odoo version receives security fixes.

| Odoo version | Supported |
|---|---|
| 19.0 | ✓ |

## Reporting a Vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Please report security issues by emailing:

**security@incubacloud.io**

Include in your report:
- A description of the vulnerability and its potential impact
- Steps to reproduce or a proof-of-concept
- Affected module(s) and Odoo version

We will acknowledge your report within **48 hours** and aim to provide a fix or mitigation plan within **14 days** for critical issues.

We will credit researchers who responsibly disclose vulnerabilities unless they prefer to remain anonymous.

## The admin user's password

From 1.0.234 the panel gives the Odoo admin user of each instance a random
password, which it does not keep, when it creates or loads the instance's
database, and gives one to the existing instances still using a default
password. Sign in with Connect and set your own password in Odoo
(Settings → Users).

An instance that was using a default password gets the alert «The admin
user still had Odoo's default password». On it, an administrator should
review in Odoo:

- the users with access to Settings, and any they do not know;
- the API keys of those users: an API key created with the admin user
  keeps working after its password changes, and is removed from the user's
  preferences (Account Security);
- the admin user's recent sign-ins, and the devices signed in with it
  where its Odoo lists them (Account Security).

To check an instance by hand, ask it whether its admin user still signs in
with Odoo's default password. On the instance's server, from its
directory:

```sh
docker compose exec -T odoo python3 -c 'import xmlrpc.client as x; print(x.ServerProxy("http://localhost:8069/xmlrpc/2/common").authenticate("prod", "admin", "admin", {}))'
```

`False` means it does not; a number means it does: sign in with Connect and
change the admin user's password. Replace `prod` with the instance's
database name if it is another, and `python3` with `python` on Odoo 10.0
and older (with `xmlrpclib` in place of `xmlrpc.client`).
