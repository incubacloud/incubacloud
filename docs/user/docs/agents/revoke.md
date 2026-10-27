# Revoke an agent

!!! info "IncubaCloud SaaS"
    Open **Connected agents** in your IncubaCloud account (the panel's
    *Connect an agent* page links it). It lists each agent with its panel,
    when you allowed it and when it was last used. **Revoke** cuts it off:
    its access ends within five minutes, and it cannot renew it.

    Signing out of your IncubaCloud account does **not** disconnect your
    agents: they do not depend on your browser's session. An agent unused
    for 30 days is cut off on its own.

On a self-hosted panel:

- **With API keys**, delete the agent's key in your preferences
  (**Security**, **API Keys**): its next call fails.
- **With your own OAuth provider**, revoke the agent's grant there; its
  access ends when its current token expires.

Whatever the agent holds, a user who is deactivated, or who no longer has
a role on the panel, is refused at once.
