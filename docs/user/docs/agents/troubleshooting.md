# Troubleshooting

| What you see | Why, and what to do |
|---|---|
| `401` | The agent has no valid credential: sign in again from the client (`/mcp` in Claude Code), or check the API key — revoked or expired keys are refused. |
| `403` *Sign in to the panel once to create your user, then connect the agent again* | Your user does not exist on this panel yet: open the panel in the browser once, then connect the agent again. |
| `403` with another reason | Your user is inactive, is not an internal user, or has no role on the panel. |
| *On this panel agents connect with OAuth* | This panel does not accept API keys: connect with OAuth. |
| The client only offers API keys or headers | Your client does not support OAuth: use another client or, on a self-hosted panel, an API key. |
| *Agents cannot connect to this panel yet* (on the *Connect an agent* page) | The panel has no OAuth provider for agents and does not accept API keys. On IncubaCloud SaaS, contact support. |
| *This panel's MCP address is not configured yet* or *is not a plain web address* | The page shows no command. A manager sets or corrects the MCP address in **Settings**, **Agents**. On IncubaCloud SaaS, contact support. |
| `503` with `Retry-After` | The sign-in service could not be reached, this panel's sign-in for agents is not completely configured or, on IncubaCloud SaaS, the panel was asleep or restarting: the first call wakes it up and may take up to a minute. Try again shortly; if it lasts, contact support or check the OAuth provider. |
| `429` *Too many failed authentications from this address* | 60 sign-ins from your address failed within a minute. Wait a minute and fix the credential: sign in again, or check the API key. |
| *Your agents made too many calls in the last minute* | Your agents together passed the per-user limit. Wait a minute; managers can raise it in **Settings**, **Rates**. |
| *Secrets are changed in the panel* | The agent tried to send a password, key or token: set it in the panel. |
| *This confirmation has expired* | More than ten minutes passed, or the approval was for another action: ask for the action again. |
| *This action needs your approval: open … and ask for it again* | Your client cannot show the question itself: open the link, approve, and ask the agent to try again. |
| Claude cannot reach the panel | Claude connects from Anthropic's servers: the panel must be reachable from the internet. |
| `404` on `/mcp` (self-hosted) | No database was selected: configure a `dbfilter` by host name. |
