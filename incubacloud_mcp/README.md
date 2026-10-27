# IncubaCloud MCP

Lets AI agents (Claude Code, claude.ai, Claude Desktop, Cursor, VS Code,
Codex…) operate the panel through the Model Context Protocol, at `POST /mcp`,
as the user who connected them.

- **Who:** OAuth access tokens of the configured issuer, or the panel's own
  API keys where allowed. Never the browser's cookie, never the superuser.
- **What:** fifty-eight tools over the panel's own routes (three more with
  `incubacloud_mcp_pitr`), listed by role.
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
