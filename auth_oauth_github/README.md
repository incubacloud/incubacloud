# GitHub OAuth Authentication

Adds **Sign in with GitHub** to the Odoo login page.

## Why a module is needed

Stock `auth_oauth` cannot drive GitHub on its own:

| Stock `auth_oauth` assumes | GitHub actually does |
| --- | --- |
| Implicit flow (`response_type=token`) | Only the authorization **code** flow, exchanged server-side with a client secret |
| An OpenID Connect identity (`id_token` / `sub`) | No OIDC at all — identity is read from the REST API |
| Access token sent as a query string | Rejects that; requires an `Authorization: Bearer` header |
| The email in the profile is usable | `/user` returns only the *public* email, often empty and never verified |

This module supplies exactly those four missing pieces and then hands the
result to the ordinary `res.users._auth_oauth_signin`, so signup, account
linking and any override already in the database keep working unchanged.

## Verified email only

The address is read from `GET /user/emails` and must be **verified**
(primary preferred). A GitHub account with no verified address is refused.
`email_verified` is only ever set from GitHub's own flag, so anything
downstream that treats the claim as proof of mailbox ownership stays
honest.

## Setup

You register **one** OAuth App for the whole Odoo instance — not one per
user. It is a five-minute job done once.

1. Go to <https://github.com/settings/developers> →
   *OAuth Apps* → **New OAuth App**.
   (For an organisation-owned app:
   *Organisation settings → Developer settings → OAuth Apps*.)
2. Fill in:
   - **Application name** — whatever your users should see on GitHub's
     authorization screen, e.g. `Acme Odoo`.
   - **Homepage URL** — `https://<your-odoo>`
   - **Authorization callback URL** — this one must be exact:
     ```
     https://<your-odoo>/auth_oauth/github/callback
     ```
3. Press **Register application**, then **Generate a new client secret**.
   Copy the **Client ID** and the secret (GitHub shows the secret once).
4. In Odoo, go to *Settings → Users & Companies → OAuth Providers* and
   open the **GitHub** record (this module ships it, disabled).
   - Paste **Client ID** and **Client Secret**.
   - Tick **Allowed**.
   - Leave *GitHub authorization code flow* ticked.
5. Log out. The **Sign in with GitHub** button is on `/web/login`.

> Odoo must be reachable over **HTTPS** at the same host as the callback
> URL, and `web.base.url` should match it. GitHub compares the callback
> sent during login against the one registered, and refuses a mismatch.

### There is no "create the app for me" button, and cannot be

GitHub has **no API to create an OAuth App** — they are made by hand in
the web UI, full stop. (The manifest flow that some integrations use to
create an app in one click is for *GitHub Apps*, a different kind of app
that gets **installed** into an account or organisation.) Since the app
here is registered once by the administrator and never by end users, the
manual registration above is the whole cost.

A **GitHub App** can also drive this login — same endpoints, its own
Client ID/Secret, plus the *Email addresses* account permission. It is
not what this module ships for, because installation can require
organisation-owner approval, which would put an approval step in front of
people who only want to log in.

### Scopes and organisations

The default scope is `read:user user:email`. Nothing about organisations
or repositories is requested, so users only see a plain *Authorize*
screen: nothing is installed, and organisation OAuth-app restrictions and
SAML do not gate signing in.

## Troubleshooting

Failures always land back on `/web/login?oauth_error=N` — never a
traceback — so the server log is where the reason is. Look for lines
tagged `[github-oauth]`.

| What you see | What it means |
| --- | --- |
| GitHub says *"The redirect_uri MUST match the registered callback URL"* | The **Authorization callback URL** in the OAuth App is not exactly `https://<your-odoo>/auth_oauth/github/callback`. A trailing slash or `http://` is enough to break it. |
| Back at the login page, log says `token exchange refused` | Wrong **Client Secret**, or the code was reused/expired. Generate a fresh secret and paste it again. |
| Log says `account <id> has no verified email — refusing` | That GitHub account has no **verified** address. The user verifies their email on GitHub and retries. This is deliberate: an unverified address proves nothing about who is signing in. |
| Log says `nonce mismatch` | The login page and the callback did not share a session — usually a stale tab, or cookies being dropped between the two. Reload `/web/login` and try again. |
| Log says `GitHub provider is missing client credentials` | Client ID or Client Secret is empty on the provider record. |
| No GitHub button on the login page | The provider record is not **Allowed**. |
| A new user cannot sign up, only existing ones link | Signup on the database is closed (`auth_signup.invitation_scope`). That is Odoo's own setting, not this module's. |

## Notes

- Ships **disabled and without credentials**; a module upgrade never
  overwrites them (`noupdate="1"`).
- Every server-side call goes to a fixed GitHub URL held in a module
  constant, never to a value read from the database.
- The login button carries a nonce that the callback verifies, so a
  callback with a state the visitor did not initiate is refused.
- The client secret is restricted to `base.group_system` and is stripped
  from the provider data the login page renders.
- Coexists with OCA `auth_oidc`: `client_secret` is the same field name
  and type, and this module adds no `flow` field of its own.
