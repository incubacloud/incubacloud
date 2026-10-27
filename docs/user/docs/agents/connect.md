# Connect an agent

Open the user menu (your name, top right) and choose **Connect an agent**.
The page shows this panel's **MCP address**, with a button to copy it, and
the steps for each client, with the address already in every command.

How your agent signs in depends on the panel:

- **With OAuth** (IncubaCloud SaaS, or a self-hosted panel with an OAuth
  provider for agents): the first time, the agent opens a page where you
  sign in and allow it, as you do in the browser.
- **With an API key** (a self-hosted panel without one): you create a key
  and the agent sends it with every call. See
  [Self-hosted panel without OAuth](#self-hosted-panel-without-oauth).

!!! info "IncubaCloud SaaS"
    Your agent signs in with your IncubaCloud account. Your user must exist
    on the panel: if you were invited and never opened the panel, open it
    once first.

## Claude Code

Run, with your panel's address:

```bash
claude mcp add --transport http incubacloud 'https://<your panel>/mcp'
```

Then run `/mcp` in Claude Code and choose *incubacloud* to sign in.

## Claude and Claude Desktop

1. In Claude, open **Customize**, then **Connectors**, and choose **+ Add**,
   then **Add custom connector**.
2. Name it, for example *IncubaCloud*, paste the panel's MCP address and
   choose **Continue**.
3. Keep the sign-in settings Claude detects, choose **Add**, and sign in.

To use it in a conversation, choose **+** at the bottom left of the chat,
then **Connectors**, and switch it on.

On a Team or Enterprise plan, an owner adds the connector once, in
**Organization settings**, **Connectors** (**Add**, **Custom**, **Web**);
then each member finds it in **Customize**, **Connectors**, and chooses
**Connect**.

!!! note "The panel must be reachable from the internet"
    Claude connects from Anthropic's servers, not from your computer. A
    self-hosted panel on a private network, behind a VPN or a firewall
    that blocks them cannot be added this way.

## Cursor

Choose **Add to Cursor** on the panel's page, or add this to
`~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "incubacloud": {
      "url": "https://<your panel>/mcp"
    }
  }
}
```

## VS Code

Run:

```bash
code --add-mcp '{"name":"incubacloud","type":"http","url":"https://<your panel>/mcp"}'
```

## Codex

Add this to `~/.codex/config.toml`:

```toml
[mcp_servers.incubacloud]
url = "https://<your panel>/mcp"
```

Then run this to sign in:

```bash
codex mcp login incubacloud
```

## Other clients

Add a remote MCP server of type HTTP (streamable HTTP) with the panel's MCP
address. The client must support OAuth, or, on a self-hosted panel without
OAuth, send an API key in a header.

## Self-hosted panel without OAuth

Without an OAuth provider for agents, the page says *This panel accepts API
keys only*, and every command it shows carries the key.

1. Open your preferences (the page links them), **Security**, **API Keys**,
   and choose **Add API Key**. Treat it like a password.
2. Use the commands the page shows. Where one says `<API_KEY>`, put your
   key; where it names the environment variable `INCUBACLOUD_API_KEY`, set
   that variable to your key before you start the client.

| Client | How it sends the key |
|---|---|
| Claude Code | `claude mcp add … --header "Authorization: Bearer <API_KEY>"` |
| Claude and Claude Desktop | When adding the connector, choose **No sign in**; under **Request headers**, add `Authorization` with the value `Bearer <API_KEY>`. On an individual plan only: a connector an owner adds in Organization settings is shared by every member, and so would your key be. |
| Cursor | `~/.cursor/mcp.json` with `"headers": {"Authorization": "Bearer ${env:INCUBACLOUD_API_KEY}"}`: Cursor reads the key from the environment. There is no one-click link: it would carry the key. |
| VS Code | `code --add-mcp` with `"headers": {"Authorization": "Bearer <API_KEY>"}` |
| Codex | `bearer_token_env_var = "INCUBACLOUD_API_KEY"` in `config.toml`: Codex reads the key from the environment. |
| Other clients | The header `Authorization: Bearer <API_KEY>`. |

!!! info "IncubaCloud SaaS"
    The panels of IncubaCloud SaaS accept OAuth only: an API key is
    refused, whoever created it.

A server with several databases needs a `dbfilter` that selects the
database by host name; without it, `/mcp` answers 404.
