# IncubaCloud MCP

Lets AI agents (Claude Code, claude.ai, Claude Desktop, Cursor, VS Code,
Codex…) operate the panel through the Model Context Protocol, at `POST /mcp`,
as the user who connected them.

- **Who:** OAuth access tokens of the configured issuer, or the panel's own
  API keys where allowed. Never the browser's cookie, never the superuser.
- **What:** sixty-four tools, fifty-eight over the panel's own routes and six
  on the project's GitHub repository (three more with `incubacloud_mcp_pitr`),
  listed by role.
- **Code** (developer): `repo_read`, `repo_create_branch`, `repo_write_files`,
  `repo_open_pull_request`, `repo_create`, `project_connect_repo`. Reads only
  repositories of a project or an instance the user reads, writes only to those
  of a project; `repo_create` and `project_connect_repo` add one, after the
  user approves, under its canonical address. Reads go through the App, the
  token, then anonymously; writes through the App, then the token. A new branch
  needs no approval; writing to one that existed before does, and so does
  writing to a branch a production deploys from (the approval names it),
  anything in a public repository, and a pull request that makes a preview of
  production. Never under `.git/` or `.github/`, never a panel secret in a
  file, at most 180,000 characters per commit.
- **Careful with:** destructive and security-sensitive tools wait for the
  user's approval, bound to the user and to the agent's credential, ten
  minutes, once; secrets never go in or out; logs, mail and command output are
  marked as untrusted data. The approval protects from the model's mistakes and
  from injected instructions, not from a malicious client.
- **Where to connect:** the user menu's *Connect an agent*.

Configuration: Settings → Agents (the agents' issuer and the MCP address) and
Settings → Rates (calls per user and minute). A self-hosted server with
several databases needs a `dbfilter` by host name. Needs `joserfc`.

Reference: [`docs/mcp.md`](../docs/mcp.md). User guide:
<https://docs.incubacloud.io/agents/>
