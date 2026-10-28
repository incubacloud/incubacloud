# MCP server (`incubacloud_mcp`)

AI agents — Claude Code, claude.ai, Claude Desktop, Cursor, VS Code, Codex —
operate the panel through the [Model Context Protocol](https://modelcontextprotocol.io).
They act as the user who connected them: the user's role, the user's project
rules, never more. Destructive and security-sensitive actions wait for that
user's approval; secrets never go in or out.

`incubacloud_mcp` is installable on its own (a self-hosted panel installs it
if it wants it). `incubacloud_mcp_pitr` installs itself where continuous
recovery is installed too, and adds its tools. Dependency: `joserfc`
(`requirements.txt`); a self-hosted install rebuilds its image.

User guide: [docs.incubacloud.io/agents](https://docs.incubacloud.io/agents/)
(`docs/user/docs/agents/`).

## Endpoint and protocol

`POST /mcp` on the panel's own domain (`type='http'`, `auth='none'`,
`csrf=False`). One JSON-RPC message per POST — a batch is an invalid
request — and JSON answers only, never SSE.

| Request | Answer |
|---|---|
| `GET`, `DELETE` | 405, `Allow: POST` |
| No database (a server with several databases and no `dbfilter` by host name) | 404 |
| An `Origin` that is not the panel's (from the MCP address) | 403 |
| A `Content-Type` other than `application/json` | 415 |
| No `Content-Length` (a chunked body) | 411 |
| A body over 256 KiB | 413 |
| Not JSON, or nested deeper than 32 levels | 400 (`-32700`, `-32600`) |
| A notification (no `id`) | 202, no body |

The session cookie is ignored: only `Authorization: Bearer` counts.
`auth='user'` would accept the panel's cookie, and `auth='bearer'` falls back
to it without the header: with `SameSite=Lax` and panels sharing a
registrable domain, a cookie-authenticated endpoint is CSRF from any sibling
subdomain.

| Version | How the client talks | State |
|---|---|---|
| 2026-07-28 | Every request carries `io.modelcontextprotocol/protocolVersion`, `clientCapabilities` and `clientInfo` in `_meta`, and the headers `MCP-Protocol-Version`, `Mcp-Method` and, for `tools/call`, `Mcp-Name` (`=?base64?…?=` decoded). Headers that do not mirror the body: 400 `-32020`. Unknown version: 400 `-32022` with `supported`. Unknown method: 404 `-32601`. | None. `server/discover` gives the versions, the capabilities and the identity. |
| 2025-11-25, 2025-06-18, 2025-03-26 | `initialize`, then `Mcp-Session-Id` on every request; `ping` answers. | The session id holds the version, the client's elicitation modes, its name, the user and the time, HMAC-signed (`incubacloud.mcp.secret`, label `session`); the server stores nothing. Forged, another user's, of a version no longer spoken or older than 24 h: 404 (the client initializes again). Missing: 400. |

Identity: `incubacloud-panel`, with the module's version. `tools/list` is
sorted by name, lists only the tools the user's role reaches and, on
2026-07-28, carries `ttlMs` (300 000) and `cacheScope: "private"`.

## Credentials

| Credential | When | Checks |
|---|---|---|
| OAuth access token | An issuer is configured (`cloud.settings.mcp_oauth_issuer`). | RS256 only; header `typ: at+jwt` (RFC 9068); `iss` = the issuer; `aud` = this panel's canonical MCP address; `exp`, `nbf`, `iat` with 60 s of leeway; `sub`, `jti` and `client_id` present. Keys: the `jwks_uri` of the issuer's metadata (`/.well-known/oauth-authorization-server`, else `/.well-known/openid-configuration`, whose `issuer` must match), cached an hour; the key set cached ten minutes, fetched again for an unknown `kid` at most every 30 s per process, and kept for a day when the issuer is down. Downloads are bounded in size and time and follow no redirect. User: the `auth.oauth.provider` whose `issuer` is the token's, and its user with `oauth_uid = sub`. |
| Odoo API key | Where the panel allows them (`cloud.settings._mcp_api_keys_allowed()`; a tenant panel with its single sign-on refuses them). | `res.users.apikeys._check_credentials(scope='rpc')`. Revoked or expired: 401. |

A credential longer than 8 KB is refused before it is read. Answers: no or
bad credential 401 (with an issuer:
`WWW-Authenticate: Bearer resource_metadata="https://<panel>/.well-known/oauth-protected-resource/mcp"`;
without: `Bearer realm="incubacloud-panel"`). A good credential for a user
who cannot use the panel — not linked yet ("Sign in to the panel once to
create your user…"), inactive, not internal, no cloud role — 403, with the
reason and no challenge. The issuer unreachable, or no enabled provider for
it, 503 with `Retry-After` (API keys keep working). Sixty failed
authentications a minute from one address (an IPv6 /64) block it with 429 —
only where the installation declares its trusted proxies: without them the
address may be the proxy's, shared by every client, so going over is only
logged. The request then runs as the user (`request.update_env`, the user's
language): never as the superuser.

`/.well-known/oauth-protected-resource/mcp` and
`/.well-known/oauth-protected-resource` publish RFC 9728's document
(`resource`, `authorization_servers`, `bearer_methods_supported`,
`resource_name`) when an issuer and an MCP address are configured, and
answer 404 otherwise.

## Configuration

| Setting | Where | Notes |
|---|---|---|
| Agents' OAuth issuer | Settings → Agents (`cloud.settings.mcp_oauth_issuer`) | An https URL without credentials, query or fragment. Empty: API keys only. Needs an enabled `auth.oauth.provider` with the same `issuer` (with or without its final slash). |
| MCP address | Settings → Agents (`incubacloud.mcp.resource`) | Canonical form: lower-case scheme and host, no default port, no trailing slash, nothing after the path; a host and a path a shell would not read (letters, digits, `.`, `_`, `-`; `~` and `%` in the path), since *Connect an agent* pastes it into commands. Set at install from `web.base.url` + `/mcp`, never overwritten. On a tenant panel the master pushes it and Settings shows it read-only. |
| Agent calls per user and minute | Settings → Rates (`rate_limit_mcp_user_per_min`) | 120 by default (zero or less falls back to it); counts every tool call of all the user's agents. |
| HMAC key | `incubacloud.mcp.secret` | 32 random bytes, created at install. Signs session ids and approval states, with a label each. |

### Using your own OAuth provider (self-hosted)

The provider must:

- let MCP clients register: Client ID Metadata Documents, or dynamic client
  registration (RFC 7591) for older clients;
- accept the `resource` parameter (RFC 8707) and put it in `aud`;
- issue refresh tokens to public clients with PKCE;
- sign access tokens with RS256 as RFC 9068 JWTs (`typ: at+jwt`,
  `client_id`, `jti`), and publish its keys (`jwks_uri`) in its metadata
  document.

Then create an `auth.oauth.provider` with its `issuer`, link each user
(`oauth_uid` = the provider's `sub`), and set the issuer in Settings.

## Tools

Every tool calls the panel route the panel itself uses, resolved in the
database's routing map (`ir.http.routing_map()`, so a module's override
applies), and runs inside a savepoint: a refusal (`{ok: false}`) keeps what
the route wrote, as in the panel; an exception rolls it back. Arguments are
checked against the tool's schema: an unknown key is an error, not a silent
drop. A tool above the user's role is not listed, and a call to it gets the
role check's message. Operations that queue a job answer at once with its
`job_id`, its state and what it waits behind; `get_job` follows it.

| Tools | Minimum role | Destructive | Approval |
|---|---|---|---|
| `panel_overview`, `search`, `list_projects`, `get_project`, `list_instances`, `get_instance`, `list_hosts`, `get_job`, `list_alerts`, `get_pitr` (bridge) | stakeholder | | |
| `instance_login_link` | stakeholder (the connect route decides per instance and user) | | for the link |
| `compare_config`, `github_repo_info` | consultant | | |
| `update_project`, `import_project`, `create_instance`, `update_instance`, `keep_staging`, `deploy_instance`, `rebuild_instance`, `resolve_conflict`, `apply_config`, `dismiss_alerts`, `freeze_repos`, `manage_tags` | consultant | | |
| `delete_instance` | consultant (production: manager) | yes | always |
| `create_project` | project manager | | |
| `get_monitoring`, `list_backups`, `read_access_log`, `read_logs`, `staging_mailbox` | developer | | |
| `revive_instance`, `clone_to_staging`, `create_backup`, `clear_staging_mailbox` | developer | | |
| `run_command` | developer | | on production, each command |
| `repo_read` | developer | | |
| `repo_create_branch` | developer | | in a public repository |
| `repo_write_files` | developer | | a branch that existed before, one a production deploys from, a public repository |
| `repo_open_pull_request` | developer | | in a public repository, or when the project makes a preview of production for each pull request |
| `repo_create`, `project_connect_repo` | developer | | always |
| `download_backup` | developer | | for the link |
| `restore_backup`, `refresh_staging` | developer | yes | always |
| `get_host`, `get_audit_log`, `get_settings`, `browse_host_dir` | manager | | |
| `harden_host`, `import_host_instance`, `deploy_monitoring`, `configure_pitr` (bridge) | manager | | |
| `manage_host` | manager | | a change of the server's trusted proxy ranges |
| `update_settings` | manager | | security settings |
| `trust_host_key`, `run_host_command` | manager | | always |
| `delete_project`, `delete_archived_instance`, `delete_host`, `delete_backup_backend`, `move_instance`, `rollback_move`, `reset_github_app`, `purge_audit_logs`, `pitr_restore` (bridge) | manager | yes | always |

Sixty-seven tools with the bridge's three. Security settings of
`update_settings` (D2): the audit retention, the trusted proxy ranges, every
rate limit (the agents' included), the agents' issuer and the MCP address,
and whatever `get_config` hides on that panel (observability, on a tenant).
The approval says each value before and after; a value equal to the one held
once normalised asks nothing. A setting the section does not have is refused
before anyone is asked.

**Code.** The six `repo_*` / `project_connect_repo` tools (developer) work on the
project's GitHub repository, in `incubacloud.mcp.tools` (`mcp_tools_code.py`) and
`github_write.py` (`GitHubWriter`: JSON to `api.github.com`, no redirect, capped
answers). Reads go as every repository read of the panel does — the GitHub App, then
the personal access token, then anonymously. Writes go through the App and, when GitHub
refuses it, the token: the App the panel creates only reads contents, so in practice
the token writes (`repo_create` needs the token alone, and checks first that the user
may connect the repository to the project).

- **Scope.** A tool reads only a repository of a project or an instance the user reads
  and writes only one of a project (`_authorized_repo`, comparing `(owner, name)` in
  lower case); `repo_create` and `project_connect_repo` add the repository to the
  project under its canonical address.
- **The branch rule is by user.** A new branch needs no approval: `incubacloud.mcp.agent.branch`
  records the branches an agent created, per user, so a branch any agent of the user
  created counts as the agent's, from another conversation or client. Writing to a
  branch that existed before asks (one whose existence cannot be told counts as
  existing). Whatever the branch, it asks when a production instance deploys from it
  — with Auto-Rebuild or without, frozen or not; the approval names the instance —
  and when the repository is public. Opening a pull request asks in a public
  repository, and in a project that makes a preview of production for each pull
  request (it creates a staging with a copy of production).
- **What a commit may hold.** Never a path under `.git/` or `.github/` in any case (a
  workflow runs with the repository's secrets), never a file with a panel secret in it
  (`git+ssh://git@` is not one), at most 50 files and 180,000 characters in all (they
  fit in the 256 KB of a request to `/mcp`).

**Commands.** `run_command` runs one non-interactive command in the
instance's `odoo` container with the terminal's controls (developer,
deployed and running, the terminal's rate buckets); `run_host_command` on
the server as its SSH user — the user the host console logs in as — with the
console's controls. The minute is kept where the command runs: `timeout -k 5
60 sh -c -- '<command>'` inside the container (a `docker exec` whose client
dies leaves its process running), and the client and the SSH session give
up past it as a backstop. A command killed after the minute counts as timed
out. What the command leaves running in the background is **not** stopped,
and the tools say so. The output is read a block at a time while it runs and
only its head kept (stdout up to about 54 KB, stderr 8 KB), so a command that
prints gigabytes costs no memory. Before the SSH, everything the command
writes is committed in transactions of their own: the rate buckets, the
approval it uses, its audit row (the whole command, secrets masked) and the
staging's clock.

**Backup downloads.** `download_backup` without `job_id` queues the download
(no approval); with the `job_id` of a finished one it asks for approval and
answers `/cloud/mcp/download/<token>` (`incubacloud.mcp.download`): one use,
15 minutes, for the user who approved it. The link is spent before anything
else, in one statement; then the user's developer role and access to the job
are checked again, and a refusal spends it too. A request carrying another
user's session gets 404 and leaves the link as it was. `HEAD` answers 405
without spending it (link previews send one). A file gone from the filestore
answers 410. The file is served under a plain name, `no-store` and
`no-referrer`, and the use is audited (`Agent: backup downloaded`). The
panel's own `/web/content` link is never given: it needs the browser's
cookie.

**Sign-in links.** `instance_login_link` without `user_id` lists the
instance's users; with `user_id` it asks for approval and returns the
connect route's one-time link (60 seconds). The user's name and login are
taken from the instance, never from the agent: the tool no longer accepts a
`user_name`. The approval says the user (name, login, id), whether the
instance is production and whether the user is an administrator of it.

**Host keys.** `trust_host_key` reads the key the server presents now
(`cloud.host._peek_known_host_key()`, no credential sent), binds the call to
its fingerprint (`expected_fingerprint`), shows the key trusted until then and
the new one, and `/cloud/trust_host_key` stores only that key: a server that
changes its key in between is not trusted.

## Routes no tool calls

Every route of `incubacloud`, `incubacloud_pitr`, `incubacloud_mcp` and any
installed module built on `incubacloud_mcp` — directly or through another
module — is classified in `incubacloud.mcp.catalog._route_policies()`: a
tool, or excluded with its reason. `test_inventory` fails on a `/cloud/*`
route without a policy.

| Routes | Why |
|---|---|
| `/cloud/get_secret`, `/cloud/save_github_pat` | Secrets never go in or out. |
| `/cloud/instance/<id>/restore` and `/restore/begin`, `/part`, `/finish` | Uploads of gigabytes belong in the panel. |
| `/cloud/terminal/*` | Interactive PTY; `run_command` replaces it. |
| `/cloud/host_terminal/*` | Interactive host console; `run_host_command` replaces it. |
| `/cloud/log/<id>`, `/cloud/log/<id>/download` | A page and a file for a browser; `get_job` replaces them. |
| `/cloud/instance/<id>/logs`, `/cloud/instance/<id>/log_archive/<name>` | A page and a file for a browser; `read_logs` replaces them. |
| `/cloud/get_user_preferences`, `/cloud/save_user_preferences`, `/cloud/telegram_detect_chat_id`, `/cloud/telegram_send_test` | The user's own notification preferences, not the operation. |
| `/cloud/ping`, `/cloud/get_langs`, `/cloud/get_users`, `/cloud/host_defaults`, `/cloud/fetch_odoojs_submodules` | Serve the panel's own forms only. |
| `/cloud/github/setup`, `/cloud/github/callback` | Install the GitHub App in a browser. |
| `/cloud/github/webhook`, `/cloud/health` | Public endpoints, not an action of the user. |
| `/cloud`, `/cloud/<path:subpath>` | The single-page application. |
| `/cloud/mcp/approve/<token>`, `/cloud/mcp/download/<token>`, `/cloud/mcp/config` | The MCP's own pages and links. |
| `/cloud/assistant/*` (the chat's fifteen routes, Settings → Assistant's included) | The assistant's chat and settings: the panel's own pages. See [`assistant.md`](assistant.md). |

## Approval

| Client | Channel |
|---|---|
| 2026-07-28 with form elicitation | `input_required` with a yes/no form and a `requestState` (HMAC-signed, label `request`: the approval, a nonce, the user, the credential, the tool, the arguments' fingerprint, the expiry). The retry runs only if all of them hold, `action` is `accept`, `confirm` is true and the approval is consumed. `decline` or `cancel` withdraws it, even if the page approved it meanwhile. |
| 2026-07-28 with URL elicitation only | `input_required` pointing at the approval page; the retry runs once the page approved it, and asks again while it waits. |
| 2025-11-25 that declared URL elicitation | Error `-32042` with `data.elicitations` (`mode: url`, `elicitationId` = the approval, `url`, `message`); the retry with the same arguments runs once it is approved. |
| Anything else | `isError` with "This action needs your approval: open <link> and ask for it again." |

`incubacloud.mcp.approval`: user, credential, tool, arguments' fingerprint,
message, channel, token hash, state (pending, approved, used, cancelled), ten
minutes. Defaults the call would resolve itself — a refresh's source, a
purge's days, a download's file, the key a server presents — are resolved
before the fingerprint, so the approval names what will run. One atomic
`UPDATE … WHERE state = … AND expires_at > now() RETURNING id` per change of
state; its use is committed at once, in a transaction of its own: of two
racing retries, one runs the action. The approval is used before the action
runs; if the action fails, the user approves again.

The assistant (`incubacloud_assistant`) adds a channel, `assistant`: a card in its chat,
answered only by the person's own request (`/cloud/assistant/confirm` or `cancel`, which
refuse a request that carries an agent's mark); the approval is bound to the user and the
conversation (`assistant:<id>`), and lasts ten minutes from the yes. Its rules are in
[`assistant.md`](assistant.md).

An approval is bound to the user **and to the credential the agent called
with**: the token's `client_id` (`client_id <id>`) or the API key itself by
its id (`API key #<id>`), never its name — two keys with the same name are two
clients. Another agent of the same user cannot use it.

The message quotes and escapes every value an agent or a user typed (as JSON
writes a string, with every non-printing character escaped), so nothing in it
can pass for the panel's own words; a command comes last, on a line of its
own, and the page shows it in a block of its own. A call the route would
refuse — the role, a production staging to refresh, a setting the section
does not have — is refused before anyone is asked.

The page (`/cloud/mcp/approve/<token>`) needs the panel session of the same
user (another user: 403; a portal user: 404), shows on GET and decides on a
POST with Odoo's CSRF token, with `frame-ancestors 'none'`,
`X-Frame-Options: DENY`, `Cache-Control: no-store` and
`Referrer-Policy: no-referrer`. The session is enough: no fresh sign-in (D5).

**Threat model (D5).** The approval protects from the model's mistakes and
from instructions injected into logs, mail or command output. It does not
protect from a malicious client, which answers `confirm: true` by itself, nor
from an agent that drives the user's browser while the panel is open. The
defence there is to connect only trusted clients and revoke the others; the
user guide says so.

## Secrets

The routes' contract is unchanged: `has_<field>`, never the value. Behind it:

- **Out.** Every answer loses any key named like an encrypted field (every
  `EncryptedChar`/`EncryptedText` of the registry: a new one is covered the
  day it is added), and every known secret is replaced by `[secreto]` wherever
  it appears in text: plain, URL-encoded and in base64, eight characters or
  more. Known secrets are every decryptable value of those fields (read with
  SQL, decrypted once per process) and `_extra_known_values()` (in core,
  `incubacloud.mcp.secret` and `database.secret`). A field that stores a
  document (`_structured_secret_fields()`: a job's credentials, the GitHub
  App's key, a server's TLS and SSH keys…) counts with its pieces: a JSON
  document's leaves under secret keys, at any depth but `login` and
  `machine`, and a PEM block's lines, at most 256 pieces. Secrets up to 4 KB
  are matched with a prefix tree; longer ones stay out of it and are replaced
  as they are, so a huge one costs milliseconds. Credentials in URLs
  (`scheme://user:password@host`) are masked too and, in captured mail, links
  with a token (sign-in, reset, signup). A secret a failing call wrote, and
  quotes in its error, is masked though the rollback takes it away.
- **In.** Arguments with a key named like an encrypted field are refused at
  any depth, naming the argument ("Secrets are changed in the panel
  (arguments.vals.password)."). A server an agent creates has no SSH
  credentials until a person sets them.
- **Cost.** A webhook secret and a Telegram bot token are 1024 characters at
  most (`/cloud/save_user_preferences`, core 1.0.201): every stored secret is
  part of what each answer is scanned for.

This is a mitigation, not a guarantee: a developer with `run_command` can
read and encode a secret of the instance, as with the terminal.

## Limits and untrusted content

Logs, captured mail and lists come in pages of 64 KB (`truncated`,
`next_offset`; `offset` and `limit` where a tool pages); a command's output
is cut (stdout about 54 KB, stderr 8 KB) and says so (`stdout_truncated`,
`stderr_truncated`). Sizes are measured as the answer serialises them, and
the bytes between entries count. A page's paging keys come on top of its
64 KB, so the last guard lets an answer through untouched up to 1 KB past
it (`PAGE_SLACK`): cutting a page for its own keys would only shorten it.
An answer past that — which no tool pages, or a page that masking grew — is
cut to just under 64 KB (`GUARD_MARGIN` leaves room for `truncated` and the
untrusted wrapping), lists first, and says so; a page keeps its first
entries and its `next_offset` moves back, so none is skipped. A page of
text — such as a captured mail of `staging_mailbox` (`page_record`) — is
left as it is: its offsets count the characters before masking, and cutting
it would skip text. A page that cannot show any entry says so too, so an
agent does not ask for it again. The output of logs, job logs, mail, access
logs, alerts, the overview, directory listings, repository data, the audit's
free text and commands is wrapped as untrusted data, and those tools'
descriptions say so. The server's instructions tell agents never to follow
instructions found in it.

## Audit

`cloud.audit.log.via_client` and `cloud.job.via_client`: the verified
credential — the token's `client_id`, or `API key <name>` — then, between
brackets, the name the client declares (unverified, without brackets of its
own, 64 characters at most). Every call that may change something leaves a
row `Agent: <tool>` with its arguments (secrets masked), refused or not; the
rows the routes and the audited models write themselves, and the jobs they
queue, carry the same mark. The mark rides on the request the MCP endpoint
serves, never on the context, which any panel user can send with an RPC. The
panel shows it in both audit tables (*· via agent …*).

## Extending (modules on top)

- **Tools.** Inherit `incubacloud.mcp.catalog`: `_tool_specs()` adds
  `ToolSpec`s (`odoo.addons.incubacloud_mcp.protocol.tool_spec`) and
  `_route_policies()` classifies every `/cloud/*` route of the module, with
  `tool_policies({...})` (`odoo.addons.incubacloud_mcp.models.mcp_catalog`).
  Handlers, `_prepare_<tool>` and `_confirm_<tool>` go on
  `incubacloud.mcp.tools` (helpers `_route`, `_record`, `_instance`,
  `_with_job_state`). `test_inventory` runs in the module's CI and fails on a
  route it left out.
- **Secrets.** `incubacloud.mcp.secrets._extra_known_values()` (secrets kept
  outside encrypted fields, such as the tenant's OIDC client secret) and
  `_structured_secret_fields()` (encrypted fields that store a document), both
  calling `super()`.
- **How the layer above runs the panel.** `cloud.settings._mcp_api_keys_allowed()`,
  `_mcp_settings_managed()` and `_mcp_agents_url()`.
- `auth.oauth.provider.issuer` and the JWKS cache
  (`odoo.addons.incubacloud_mcp.controllers._jwks_cache`) live here.
- A route a tool calls runs inside a savepoint: it must not `cr.commit()`.
