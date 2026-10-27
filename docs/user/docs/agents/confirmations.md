# Approvals

Some actions wait for you, whatever the agent asks:

- deleting a project, an instance (archived or not), a server or a backup
  destination;
- restoring a backup or continuous recovery, and replacing a staging's data
  with a copy of production;
- moving an instance or undoing a move, and resetting the GitHub App;
- trusting a server's SSH key: you see the key trusted until now and the
  one the server presents now; compare them before approving;
- a sign-in link to an instance: you see the instance's user (name, login
  and id), whether it is an administrator and whether the instance is
  production;
- a download link for a backup: you see the file and the instance;
- a command on a **production** instance, and every command on a server:
  you see the exact command;
- purging the audit log, and changing a security setting — the audit
  retention, the trusted proxies, a rate limit (the agents' included), the
  agents' issuer or the MCP address: you see each value before and after.

An action the panel would refuse anyway — your role does not allow it, the
production instance cannot be refreshed — is refused before you are asked.

## How you are asked

It depends on your client:

| Client | What you see |
|---|---|
| Clients that show forms | A yes/no question inside the client, saying what will happen. |
| Clients that open links | The client opens the panel's approval page; once you approve, it goes on. |
| Other clients | The agent gives you a link to the panel's approval page; after approving, ask it to try again. |

The approval page needs your panel session: sign in as yourself, if you
are not. Another user cannot answer it, and you cannot answer theirs. It
shows what will happen — a command in a block of its own — with
**Approve** and **Cancel**. Cancelled, the agent does not do it.

An approval lasts **ten minutes** and is used **once**, by the agent that
asked: the same action again asks again, different values ask anew, and
another of your agents (another client or API key) cannot use it.

!!! warning "What an approval protects you from"
    It protects you from the agent's mistakes and from instructions hidden
    in a log or an email that the agent read. It does **not** protect you
    from a client you should not trust — such a client can answer "yes" by
    itself — nor from an agent that drives your browser while your panel
    is open: it can open the page and approve. Connect only clients you
    trust, and [revoke](revoke.md) any you stop trusting.
